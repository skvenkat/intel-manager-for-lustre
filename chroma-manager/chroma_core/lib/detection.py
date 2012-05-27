#
# ========================================================
# Copyright (c) 2012 Whamcloud, Inc.  All rights reserved.
# ========================================================


from django.db import transaction

from chroma_core.lib.lustre_audit import audit_log, normalize_nids
from chroma_core.models import NoLNetInfo
from chroma_core.models.event import LearnEvent
from chroma_core.models.filesystem import ManagedFilesystem
from chroma_core.models.host import ManagedHost, VolumeNode, Nid
from chroma_core.models.target import ManagedMgs, ManagedTargetMount, ManagedTarget, FilesystemMember, ManagedMdt, ManagedOst


class DetectScan(object):
    def run(self, all_hosts_data):
        """:param all_hosts_data: Dict of ManagedHost to detect-scan output"""

        # Must be run in a transaction to avoid leaving invalid things
        # in the DB on failure.
        assert transaction.is_managed()

        self.all_hosts_data = all_hosts_data

        # Create ManagedMgs objects
        audit_log.debug(">>learn_mgs_targets")
        self.learn_mgs_targets()

        # Create ManagedTargetMount objects
        audit_log.debug(">>learn_mgs_target_mounts")
        self.learn_target_mounts()

        # Create ManagedMdt and ManagedOst objects
        audit_log.debug(">>learn_fs_targets")
        self.learn_fs_targets()

        # Create ManagedTargetMount objects
        audit_log.debug(">>learn_target_mounts")
        self.learn_target_mounts()

        # Clean up:
        #  Remove any targets which don't have a primary mount point
        for klass in [ManagedMdt, ManagedOst]:
            for t in klass.objects.all():
                if not t.managedtargetmount_set.filter(primary = True).count():
                    audit_log.warning("Found no primary mount point for target %s" % t)
                    ManagedTarget.delete(t.id)

        #  Remove any Filesystems with zero MDTs or zero OSTs
        for fs in ManagedFilesystem.objects.all():
            if not ManagedMdt.objects.filter(filesystem = fs).count():
                audit_log.warning("Found no MDTs for filesystem %s" % fs.name)
                ManagedFilesystem.delete(fs.id)
            elif not ManagedOst.objects.filter(filesystem = fs).count():
                audit_log.warning("Found no OSTs for filesystem %s" % fs.name)
                ManagedFilesystem.delete(fs.id)

    def _nids_to_mgs(self, host, nid_strings):
        """nid_strings: nids of a target.  host: host on which the target was seen.
        Return a ManagedMgs or raise ManagedMgs.DoesNotExist"""
        if set(nid_strings) == set(["0@lo"]) or len(nid_strings) == 0:
            return ManagedMgs.objects.get(managedtargetmount__host = host)

        from django.db.models import Count
        nids = Nid.objects.values('nid_string').filter(lnet_configuration__host__not_deleted = True, nid_string__in = nid_strings).annotate(Count('id'))
        unique_nids = [n['nid_string'] for n in nids if n['id__count'] == 1]

        if not len(unique_nids):
            audit_log.warning("nids_to_mgs: No unique NIDs among %s!" % nids)

        hosts = list(ManagedHost.objects.filter(lnetconfiguration__nid__nid_string__in = unique_nids).distinct())
        try:
            mgs = ManagedMgs.objects.distinct().get(managedtargetmount__host__in = hosts)
        except ManagedMgs.MultipleObjectsReturned:
            audit_log.error("Unhandled case: two MGSs have mounts on host(s) %s for nids %s" % (hosts, unique_nids))
            # TODO: detect and report the pathological case where someone has given
            # us two NIDs that refer to different hosts which both have a
            # targetmount for a ManagedMgs, but they're not the
            # same ManagedMgs.
            raise ManagedMgs.DoesNotExist

        return mgs

    def is_primary(self, host, local_target_info):
        if host.lnetconfiguration.state != 'nids_known':
            raise NoLNetInfo("Cannot setup target %s without LNet info" % local_target_info['name'])

        local_nids = set(host.lnetconfiguration.get_nids())

        if not 'failover.node' in local_target_info['params']:
            # If the target has no failover nodes, then it is accessed by only
            # one (primary) host, i.e. this one
            primary = True
        elif len(local_nids) > 0:
            # We know this hosts's nids, and which nids are secondaries for this target,
            # so we can work out whether we're primary by a process of elimination
            failover_nids = []
            for failover_str in local_target_info['params']['failover.node']:
                failover_nids.extend(failover_str.split(","))
            failover_nids = set(normalize_nids(failover_nids))

            primary = not (local_nids & failover_nids)
        else:
            raise NoLNetInfo("Host %s has no NIDS!" % host)

        return primary

    def is_valid(self):
        for host, host_data in self.all_hosts_data.items():
            try:
                assert(isinstance(host_data, dict))
                assert('mgs_targets' in host_data)
                assert('local_targets' in host_data)
                # TODO: more thorough validation
                return True
            except AssertionError:
                return False

    def target_available_here(self, host, mgs, local_info):
        target_nids = []
        if 'failover.node' in local_info['params']:
            for failover_str in local_info['params']['failover.node']:
                target_nids.extend(failover_str.split(","))

        if local_info['mounted']:
            return True

        if mgs:
            mgs_host = mgs.primary_server()
            fs_name, target_name = local_info['name'].rsplit("-", 1)
            try:
                mgs_target_info = None
                for t in self.all_hosts_data[mgs_host]['mgs_targets'][fs_name]:
                    if t['name'] == local_info['name']:
                        mgs_target_info = t
                if not mgs_target_info:
                    raise KeyError
            except KeyError:
                audit_log.warning("Saw target %s on %s:%s which is not known to mgs %s" % (local_info['name'], host, local_info['devices'], mgs_host))
                return False
            primary_nid = mgs_target_info['nid']
            target_nids.append(primary_nid)

        target_nids = set(normalize_nids(target_nids))
        if set(host.lnetconfiguration.get_nids()) & target_nids:
            return True
        else:
            return False

    def _target_find_mgs(self, host, local_info):
        # Build a list of MGS nids for this local target
        tgt_mgs_nids = []
        try:
            # NB I'm not sure whether tunefs.lustre will give me
            # one comma-separated mgsnode, or a series of mgsnode
            # settings, so handle both
            for n in local_info['params']['mgsnode']:
                tgt_mgs_nids.extend(n.split(","))
        except KeyError:
            # 'mgsnode' doesn't have to be present
            pass

        tgt_mgs_nids = set(normalize_nids(tgt_mgs_nids))
        return self._nids_to_mgs(host, tgt_mgs_nids)

    def learn_target_mounts(self):
        for host, host_data in self.all_hosts_data.items():
            # We will compare any found target mounts to all known MGSs
            for local_info in host_data['local_targets']:
                debug_id = (host, local_info['devices'][0], local_info['name'])
                targets = ManagedTarget.objects.filter(uuid = local_info['uuid'])
                if not targets.count():
                    audit_log.warning("Ignoring %s:%s (%s), target unknown" % debug_id)
                    continue

                for target in targets:
                    if isinstance(target, FilesystemMember):
                        try:
                            mgs = self._target_find_mgs(host, local_info)
                        except ManagedMgs.DoesNotExist:
                            audit_log.warning("Can't find MGS for target %s:%s (%s)" % debug_id)
                            continue
                    else:
                        mgs = None

                    if not self.target_available_here(host, mgs, local_info):
                        audit_log.warning("Ignoring %s on %s, as it is not mountable on this host" % (local_info['name'], host))
                        continue

                    try:
                        primary = self.is_primary(host, local_info)
                        audit_log.info("Target %s seen on %s: primary=%s" % (target, host, primary))
                        volumenode = self._get_volume_node(host, local_info['devices'])
                        (tm, created) = ManagedTargetMount.objects.get_or_create(target = target,
                            host = host, primary = primary,
                            volume_node = volumenode)
                        if created:
                            tm.immutable_state = True
                            tm.save()
                            audit_log.info("Learned association %d between %s and host %s" % (tm.id, local_info['name'], host))
                            self._learn_event(host, tm)
                    except NoLNetInfo:
                        audit_log.warning("Cannot set up target %s on %s until LNet is running" % (local_info['name'], host))

    def _get_volume_node(self, host, paths):
        volume_nodes = VolumeNode.objects.filter(path__in = paths, host = host)
        if not volume_nodes.count():
            audit_log.warning("No device nodes detected matching paths %s on host %s" % (paths, host))
            raise VolumeNode.DoesNotExist
        else:
            if volume_nodes.count() > 1:
                # On a sanely configured server you wouldn't have more than one, but if
                # e.g. you formatted an mpath device and then stopped multipath, you
                # might end up seeing the two underlying devices.  So we cope, but warn.
                audit_log.warning("DetectScan: Multiple VolumeNodes found for paths %s on host %s, using %s" % (paths, host, volume_nodes[0].path))
            return volume_nodes[0]

    def learn_fs_targets(self):
        for host, host_data in self.all_hosts_data.items():
            for local_info in host_data['local_targets']:
                if not local_info['mounted']:
                    continue

                name = local_info['name']
                device_node_paths = local_info['devices']
                uuid = local_info['uuid']

                if name.find("-MDT") != -1:
                    klass = ManagedMdt
                elif name.find("-OST") != -1:
                    klass = ManagedOst
                elif name == "MGS":
                    continue
                else:
                    raise NotImplementedError()

                try:
                    mgs = self._target_find_mgs(host, local_info)
                except ManagedMgs.DoesNotExist:
                    audit_log.warning("Can't find MGS for target %s on %s" % (name, host))
                    continue

                import re
                fsname = re.search("([\w\-]+)-\w+", name).group(1)
                try:
                    filesystem = ManagedFilesystem.objects.get(name = fsname, mgs = mgs)
                except ManagedFilesystem.DoesNotExist:
                    audit_log.warning("Encountered target (%s) for unknown filesystem %s on mgs %s" % (name, fsname, mgs.primary_server()))
                    return None

                try:
                    klass.objects.get(uuid = uuid)
                except ManagedTarget.DoesNotExist:
                    # Fall through, no targets with that name exist on this MGS
                    volumenode = self._get_volume_node(host, device_node_paths)
                    target = klass(uuid = uuid, name = name, filesystem = filesystem,
                        state = "mounted", volume = volumenode.volume,
                        immutable_state = True)
                    target.save()
                    audit_log.debug("%s" % [mt.name for mt in ManagedTarget.objects.all()])
                    audit_log.info("%s %s %s" % (mgs.id, name, device_node_paths))
                    audit_log.info("Learned %s %s" % (klass.__name__, name))
                    self._learn_event(host, target)

    def _learn_event(self, host, learned_item):
        from logging import INFO
        LearnEvent(severity = INFO, host = host, learned_item = learned_item).save()

    def learn_mgs_targets(self):
        for host, host_data in self.all_hosts_data.items():
            mgs_local_info = None
            for volume in host_data['local_targets']:
                if volume['name'] == "MGS" and volume['mounted'] == True:
                    mgs_local_info = volume
            if not mgs_local_info:
                audit_log.debug("No MGS found on host %s" % host)
                return

            try:
                mgs = ManagedMgs.objects.get(uuid = mgs_local_info['uuid'])
            except ManagedMgs.DoesNotExist:
                try:
                    volumenode = self._get_volume_node(host, mgs_local_info['devices'])
                except VolumeNode.DoesNotExist:
                    continue

                audit_log.info("Learned MGS %s (%s)" % (host, mgs_local_info['devices'][0]))
                # We didn't find an existing ManagedMgs referring to
                # this LUN, create one
                mgs = ManagedMgs(uuid = mgs_local_info['uuid'],
                    state = "mounted", volume = volumenode.volume,
                    name = "MGS", immutable_state = True)
                mgs.save()

            # Create Filesystem objects from the MGS config logs
            for fs_name, targets in host_data['mgs_targets'].items():
                (fs, created) = ManagedFilesystem.objects.get_or_create(name = fs_name, mgs = mgs)
                if created:
                    fs.immutable_state = True
                    fs.save()
                    audit_log.info("Learned filesystem '%s'" % fs_name)
                    self._learn_event(host, fs)
