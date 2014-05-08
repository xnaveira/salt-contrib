"""
Module that provides functions to manipulate iSCSI Enterprise Targets
that are backed by LVM. 

:maintainer: Brent Lambert <brent@enpraxis.net>
:maturity: new
:depends:    - iSCSI Enterprise Target (http://iscsitarget.sourceforge.net), 
    LVM2 
:platform: Linux
:configuration: Default configuration must be specified in the minion
    configuration file. A base IQN that will be used with the logical 
    volume name to make a full IQN for the target must be specified. Also
    specify a default volume group that will contain the logical volumes
    created and deleted by this module. And lastly specify the default
    location of the config file for iSCSITarget, so that the module 
    can update the static configuration, in case the server is rebooted::

        iscsitarget.iqn_base: 'iqn.2007-12.net.enpraxis'
        iscsitarget.volgroup: 'vg_spare'
        iscsitarget.config: '/etc/iet/ietd.conf'

ToDo::

    - Add function to set iSCSI Target parameters, users, security, etc.

"""

# System Imports
from subprocess import Popen, PIPE
import os
import logging

log = logging.getLogger(__name__)


# Helper functions

def _is_ietd_running():
    '''
    Check if the iSCSI daemon is running
    '''
    cmd = "pgrep -u root ietd"
    out = __salt__['cmd.run'](cmd)

    if not out:
        log.warn('iSCSI Daemon not running')
        return False

    return True
    

def _get_new_tid():
    '''
    Get a new Target ID, and make sure it is not in use
    '''
    tid = 1
    with open('/proc/net/iet/volume') as f:
        tids = [int(x.split(' ')[0].split(':')[-1]) for x in f.readlines() if 'tid:' in x]

        # Get a new ID based on the max
        # We avoid deleted TIDs as a stale iSCSI client may pick it up
        # If you have lots of TIDs and need to reuse deleted ones you may 
        # want to alter this
        if tids:
            tid = max(tids) + 1

    return tid            

        
def _get_tid_from_iqn(iqn):
    '''
    Get a target ID using a full IQN
    '''
    ret = 0
    with open('/proc/net/iet/volume') as f:
        lines = f.readlines()
        for x in lines:
            if iqn in x:
                ret = int(x.split(' ')[0].split(':')[-1])
                break
        else:
            log.error('Error: (proc/net/iet/volume) {0} not found'.format(iqn))

    return ret


def _get_volumes(iqn):
    '''
    Get all volumes associated with target
    '''
    with open('/proc/net/iet/volume') as f:
        lines = f.read()
        if iqn not in lines:
            return []
        config = lines.split(iqn)[-1]
        config = config.split('tid:')[0].split('\n')[1:-1]
        paths = []
        for x in config:
            paths.append(x.split('path:')[-1].rstrip())

    return paths


def _get_params(kwargs):
    '''
    Get config params
    '''
    iqn = _get_param('iqn_base', kwargs)
    vg = _get_param('volgroup', kwargs)
    config = _get_param('config', kwargs)
    if 'opt' in kwargs:
        opts = kwargs['opt'].split(',')
    else:
        opts = []

    return iqn, vg, config, opts
        

def _get_param(opt, kwargs):
    '''
    Get config option
    '''
    if opt in kwargs:
        return kwargs[opt]
    return __salt__['config.option']('iscsitarget.{0}'.format(opt))


def _rewrite_config(cf, lines):
    '''
    Rewrite the configuration file
    '''
    cf.seek(0)
    cf.write(''.join(lines))
    cf.truncate()


def _config_add_target(config, tid, fiqn):
    '''
    Add a target to the config file
    '''
    with open(config, 'a') as f:
        f.write('Target {0} {1}\n'.format(tid, fiqn))


def _config_delete_target(config, fiqn):
    '''
    Delete a target from the config file
    '''
    with open(config, 'r+') as f:
        clines = f.readlines()
        tgts = [x for x in range(len(clines)) if clines[x].lstrip().startswith('Target')]
        # Find the Target
        tgt = [x for x in tgts if fiqn in clines[x]]
        if tgt:
            # Delete the whole target
            t = tgt[0]
            while True:
                del clines[t]
                # Delete until the end, or until the next Target definition
                if t >= len(clines) or clines[t].lstrip().startswith('Target'):
                    break
        
        _rewrite_config(f, clines)
        

def _create_vol(name, size, vg):
    '''
    Create the logical volume
    '''
    # Would use the lvm.lvcreate command, but it throws away the return
    # code, and it would be dangerous to think you have created a new
    # volume, but instead pass on one that was already created.
    cmd = 'lvcreate -n {0} {1} -L {2}'.format(name, vg, size)
    out = __salt__['cmd.retcode'](cmd)
    if out:
        log.error('Error: lvcreate({0}) Could not create volume'.format(out))
        return False
    return True
                  

def _delete_vol(name, vg):
    '''
    Remove the logical volume
    '''
    # Use cmd.retcode to make sure it worked
    cmd = 'lvremove -f /dev/{0}/{1}'.format(vg, name)
    out = __salt__['cmd.retcode'](cmd)
    if out:
        log.error('Error: lvremove({0}) Could not delete volume /dev/{1}/{2})'.format(
                out, vg, name))
        return False
    return True


def _add_lun(tid, lun, path, iotype='blockio'):
    ''' 
    Add a LUN to a Target
    '''
    # Attach the logical volume to the target
    cmd = 'ietadm --op new --tid {0} --lun {1} --params Path={2},Type={3}'.format(
        tid, lun, path, iotype)
    out = __salt__['cmd.retcode'](cmd)
    if out:
        log.error('Error: ietadm({0}) Could not attach logical volume to target {1}'.format(
                out, path))
        return False
    return True


def _config_add_lun(config, fiqn, lun, vg, name, iotype='blockio'):
    '''
    Add a LUN to a Target in the config file. If the Target does not exist
    create config for it.
    '''
    nlun = '\tLun {0} PATH=/dev/{1}/{2},Type={3}\n'.format(lun, vg, name, iotype)

    with open(config, 'r+') as f:
        clines = f.readlines()
        tgts = [x for x in range(len(clines)) if clines[x].lstrip().startswith('Target')]

        # find the target
        tgt = [x for x in tgts if fiqn in clines[x]]
        if tgt:
            t = tgt[0] + 1
            while (t < len(clines) and clines[t].lstrip().startswith('Lun')):
                t += 1
            clines.insert(t, nlun)
        else:
            clines.append('Target {0}\n'.format(fiqn))
            clines.append(nlun)

        _rewrite_config(f, clines)
    

def _delete_lun(tid, lun):
    '''
    Delete a LUN from a Target
    '''
    # Remove the LUN from the target
    cmd = 'ietadm --op delete --tid {0} --lun {1}'.format(tid, lun)
    out = __salt__['cmd.retcode'](cmd)
    if out:
        log.error('Error: ietadm({0}) Could not delete LUN {1} on target {2}'.format(out, lun, tid))
        return False
    return True


def _config_delete_lun(config, fiqn, lun, rtarget=False):
    '''
    Delete a LUN configuration from a Target in the config file
    '''
    with open(config, 'r+') as f:
        clines = f.readlines()
        tgts = [x for x in range(len(clines)) if clines[x].lstrip().startswith('Target')]
        # Find the Target
        tgt = [x for x in tgts if fiqn in clines[x]]
        if tgt:
            # Delete just the LUN
            t = tgt[0] + 1
            while (t < len(clines) and clines[t].lstrip().startswith('Lun')):
                if 'Lun {0}'.format(lun) in clines[t]:
                    del clines[t]
                else:
                    t += 1

        _rewrite_config(f, clines)
            

def add_target(name, **kwargs):
    '''
    Add an iSCSI target. A target ID will be chosen automatically and 
    checked against /proc/net/iet/volume to make sure it is not in use. 
    Must provide a name to be used with the IQN base parameter to generate 
    a full IQN for use. Optional paramters include iqn_base, volgroup, 
    and iet_config. The iqn_base, volgroup and iet_config settings will 
    fall back to defaults configured in the minion configuration file if 
    not specified on the command line.

    CLI_Examples::
    
        salt \* iscsitarget.add_target test

        salt \* iscsitarget.add_target test iqn_base=iqn.2007-12.net.enpraxis

        salt \* iscsitarget.add_taerget test volgroup=vg_spare

        salt \* iscsitarget.add_target test iet_config=/dev/iet/ietd.conf
    '''

    # Check that ietd is running first
    # We do this because if the SAN is running
    # in HA mode, then we only want to make
    # changes on the active SAN
    if not _is_ietd_running():
        return 'Error: (ietd) ietd not active',

    # Get Parameters
    iqn_base, vg, config, opts = _get_params(kwargs)
    fiqn = '{0}:{1}'.format(iqn_base, name)
    tid = _get_new_tid()

    # Create the iscsi target
    cmd = 'ietadm --op new --tid {0} --params Name={1}'.format(tid, fiqn)
    out = __salt__['cmd.retcode'](cmd)
    if out:
        return 'Error: ietadm({0}) Could not create iSCSI Target {1}'.format(out, fiqn)

    # Add target to config
    _config_add_target(config, tid, fiqn)

    return name, fiqn


def delete_target(name, **kwargs):
    '''
    A function for deleting an iSCSI target definition. The name parameter is 
    the name used to generate the IQN. Optional parameters are iqn_base, volgroup, 
    iet_config. These will be read from the minion configuration file if not provided. 

    CLI Example::

        salt \* iscsitarget.delete_target test

        salt \* iscsitarget.delete_target test iqn_base=iqn-2007-12.net.enpraxis

        salt \* iscsitarget.delete_target test volgroup=vg_spare

        salt \* iscsitarget.delete_target test "config=/etc/iet/ietd.conf"
    '''
    # Check that ietd is running
    # We do this because if the SAN is running
    # in HA mode, then we only want to make
    # changes on the active SAN
    if not _is_ietd_running():
        return 'Error: (ietd) ietd not active'

    # Get parameters
    iqn_base, vg, config, opts = _get_params(kwargs)
    fiqn = '{0}:{1}'.format(iqn_base, name)
    path = '/dev/{0}/{1}'.format(vg, name)
    tid = _get_tid_from_iqn(fiqn)
    if not tid:
        return 'Error: (proc/net/iet/volume) {0} not found'.format(fiqn)
    vols = _get_volumes(fiqn)

    # Remove the target
    cmd = 'ietadm --op delete --tid {0}'.format(tid)
    out = __salt__['cmd.retcode'](cmd)
    if out:
        return 'Error: ietadm({0}) Could not delete target {1}'.format(out, fiqn)
    
    # Remove the configuration
    _config_delete_target(config, fiqn)

    return True


def add_lun(name, lun, size, **kwargs):
    '''
    Add a LUN to an existing target

    CLI Example::

        salt '*' iscsirarget.add_lun 2 10G
    '''

    # Check that ietd is running
    if not _is_ietd_running():
        return 'Error: (ietd) ietd not active',

    # Get Parameters
    iqn_base, vg, config, opts = _get_params(kwargs)
    fiqn = '{0}:{1}'.format(iqn_base, name)
    vn = '{0}_{1}'.format(name, lun)
    path = '/dev/{0}/{1}'.format(vg, vn)
    tid = _get_tid_from_iqn(fiqn)
    if not tid:
        return 'Error: (proc/net/iet/volume) {0} not found'.format(fiqn)
        
    # Create a logical volume
    if not _create_vol(vn, size, vg):
        return False

    # Add to target
    if not _add_lun(tid, lun, path):
        return False

    # Update config file
    _config_add_lun(config, fiqn, lun, vg, name)

    return True
                

def delete_lun(name, lun, **kwargs):
    '''
    Delete a LUN on an existing target

    CLI Example::

        salt \* iscsitarget.delete_lun 2
    '''

    # Check that ietd is running
    if not _is_ietd_running():
        return 'Error: (ietd) ietd not active',

    # Get Parameters
    iqn_base, vg, config, opts = _get_params(kwargs)
    fiqn = '{0}:{1}'.format(iqn_base, name)
    vn = '{0}_{1}'.format(name, lun)
    path = '/dev/{0}/{1}'.format(vg, vn)
    tid = _get_tid_from_iqn(fiqn)
    if not tid:
        return 'Error: (proc/net/iet/volume) {0} not found'.format(fiqn)

    # Delete from target
    if not _delete_lun(tid, lun):
        return False

    # Update config file
    _config_delete_lun(config, fiqn, lun)

    # Remove logical volume
    if not _delete_vol(vn, vg):
        return False

    return True


def list_volumes():
    '''
    Get iSCSI Target volume information

    CLI Example::

        salt \* iscsitarget.list_volumes
    '''
    with open('/proc/net/iet/volume') as f:
        return f.read()


def list_sessions():
    '''
    Get iSCSI Target session information

    CLI Example::

        salt \* iscsitarget.list_sessions
    '''
    with open('/proc/net/iet/session') as f:
        return f.read()

