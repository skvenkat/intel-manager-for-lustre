#
# ========================================================
# Copyright (c) 2012 Whamcloud, Inc.  All rights reserved.
# ========================================================


import json
import threading
import sys
import traceback
from dateutil import tz
import dateutil.parser

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
import django.utils.timezone

from chroma_core.lib.cache import ObjectCache
from chroma_core.models import Command, StateLock, ConfigureLNetJob, ManagedHost, ManagedMdt, FilesystemMember, GetLNetStateJob, ManagedTarget, ApplyConfParams, ManagedOst, Job, DeletableStatefulObject, StepResult, StateChangeJob
from chroma_core.services.job_scheduler.dep_cache import DepCache
from chroma_core.services.job_scheduler.lock_cache import LockCache
from chroma_core.services.job_scheduler.command_plan import CommandPlan
from chroma_core.services.log import log_register


log = log_register(__name__.split('.')[-1])


class RunJobThread(threading.Thread):
    CANCEL_TIMEOUT = 30

    def __init__(self, job_scheduler, job):
        super(RunJobThread, self).__init__()
        self.job = job
        self._job_scheduler = job_scheduler
        self._cancel = threading.Event()
        self._complete = threading.Event()

    def cancel(self):
        log.info("Job %s: cancelling" % self.job.id)
        self._cancel.set()
        log.info("Job %s: waiting %ss for run to complete" % (self.job.id, self.CANCEL_TIMEOUT))
        self._complete.wait(self.CANCEL_TIMEOUT)
        if self._complete.is_set():
            log.info("Job %s: cancel completed" % self.job.id)
        else:
            # HYD-1485: Get a mechanism to interject when the thread is blocked on an agent call
            log.error("Job %s: cancel timed out, will continue as zombie thread!" % self.job.id)

    def run(self):
        self._run()
        self._complete.set()

    def _run(self):
        log.info("Job %d: %s.run" % (self.job.id, self.__class__.__name__))

        try:
            steps = self.job.get_steps()
        except Exception, e:
            log.error("Job %d: exception in get_steps" % self.job.id)
            exc_info = sys.exc_info()
            log.error('\n'.join(traceback.format_exception(*(exc_info or sys.exc_info()))))
            self._complete.set()
            return

        step_index = 0
        finish_step = -1
        while step_index < len(steps) and not self._cancel.is_set():
            klass, args = steps[step_index]

            result = StepResult(
                step_klass = klass,
                args = args,
                step_index = step_index,
                step_count = len(steps),
                job = self.job)
            result.save()

            step = klass(self.job, args, result)

            from chroma_core.lib.agent import AgentException
            try:
                log.debug("Job %d running step %d" % (self.job.id, step_index))
                step.run(args)
                log.debug("Job %d step %d successful" % (self.job.id, step_index))

                result.state = 'success'
            except AgentException, e:
                log.error("Job %d step %d encountered an agent error" % (self.job.id, step_index))
                self._job_scheduler.complete_job(self.job, errored = True)

                result.backtrace = e.agent_backtrace
                # Don't bother storing the backtrace to invoke_agent, the interesting part
                # is the backtrace inside the AgentException
                result.state = 'failed'
                result.save()

                return

            except Exception:
                log.error("Job %d step %d encountered an error" % (self.job.id, step_index))
                exc_info = sys.exc_info()
                backtrace = '\n'.join(traceback.format_exception(*(exc_info or sys.exc_info())))
                log.error(backtrace)
                self._job_scheduler.complete_job(self.job, errored = True)

                result.backtrace = backtrace
                result.state = 'failed'
                result.save()

                return
            finally:
                result.save()

            finish_step = step_index
            step_index += 1

        if self._cancel.is_set():
            return

        # For StateChangeJobs, set update the state of the affected object
        if isinstance(self.job, StateChangeJob):
            obj = self.job.get_stateful_object()
            obj = obj.__class__._base_manager.get(pk = obj.pk)
            new_state = self.job.state_transition[2]
            obj.set_state(new_state, intentional = True)
            log.info("Job %d: StateChangeJob complete, setting state %s on %s" % (self.job.pk, new_state, obj))

        # Freshen cached information about anything that this job held a writelock on
        locks = json.loads(self.job.locks_json)
        for lock in locks:
            if lock['write']:
                lock = StateLock.from_dict(self.job, lock)
                if isinstance(lock.locked_item, DeletableStatefulObject) and not lock.locked_item.not_deleted:
                    ObjectCache.purge(lock.locked_item.__class__, lambda o: o.id == lock.locked_item.id)
                else:
                    ObjectCache.update(lock.locked_item)

        log.info("Job %d finished %d steps successfully" % (self.job.id, finish_step + 1))

        # Ensure that any changes made by this thread are visible to other threads before
        # we ask job_scheduler to advance
        with transaction.commit_manually():
            transaction.commit()

        self._job_scheduler.complete_job(self.job, errored = False)

        return


class JobCollection(object):
    def __init__(self):
        self.flush()

    def flush(self):
        self._state_jobs = {
            'pending': {},
            'tasked': {},
            'complete': {}
        }
        self._jobs = {}

    def add(self, job):
        self._jobs[job.id] = job
        self._state_jobs[job.state][job.id] = job

    def add_many(self, jobs):
        for job in jobs:
            self.add(job)

    def add_command(self, command, jobs):
        """Add command if it doesn't already exist, and ensure that all
        of `jobs` are associated with it

        """

    def get(self, job_id):
        return self._jobs[job_id]

    def update(self, job, new_state, **kwargs):
        del self._state_jobs[job.state][job.id]

        Job.objects.filter(id = job.id).update(state = new_state, **kwargs)
        job.state = new_state

        self._state_jobs[job.state][job.id] = job

    def update_many(self, jobs, new_state):
        for job in jobs:
            del self._state_jobs[job.state][job.id]
            job.state = new_state
            self._state_jobs[job.state][job.id] = job

        Job.objects.filter(id__in = [j.id for j in jobs]).update(state = new_state)

    @property
    def ready_jobs(self):
        result = []
        for job in self._state_jobs['pending'].values():
            wait_for_ids = json.loads(job.wait_for_json)
            complete_job_ids = [j.id for j in self._state_jobs['complete'].values()]
            if not set(wait_for_ids) - set(complete_job_ids):
                result.append(job)

        if len(result) == 0 and len(self.pending_jobs) == 0 and len(self.tasked_jobs) == 0:
            # A quiescent state, flush the collection (avoid building up an indefinitely
            # large collection of complete jobs)
            log.debug("%s.flush" % (self.__class__.__name__))
            self.flush()

        return result

    @property
    def pending_jobs(self):
        return self._state_jobs['pending'].values()

    @property
    def tasked_jobs(self):
        return self._state_jobs['tasked'].values()


class JobScheduler(object):
    """A single instance of this class is created within the `job_scheduler` service.

    It is on the receiving end of RPCs (JobSchedulerRpc) and also is called
    by the handler for NotificationQueue


    """
    def __init__(self):
        self._lock = threading.RLock()
        """Globally serialize all scheduling operations: within a given cluster, they all potentially
        interfere with one another.  In the future, if this class were handling multiple isolated
        clusters then they could take a lock each and run in parallel

        """

        self._lock_cache = LockCache()
        self._job_collection = JobCollection()

        self._run_threads = {}  # Map of job ID to RunJobThread

    @transaction.commit_on_success
    def _run_next(self):
        ready_jobs = self._job_collection.ready_jobs

        log.info("run_next: %d runnable jobs of (%d pending, %d tasked)" % (
            len(ready_jobs),
            len(self._job_collection.pending_jobs),
            len(self._job_collection.tasked_jobs)))

        dep_cache = DepCache()
        ok_jobs = self._check_jobs(ready_jobs, dep_cache)
        self._job_collection.update_many(ok_jobs, 'tasked')

        for job in ready_jobs:
            self._spawn_job(job)

    def _check_jobs(self, jobs, dep_cache):
        """Return the list of jobs which pass their checks"""
        ok_jobs = []

        for job in jobs:
            try:
                deps_satisfied = job._deps_satisfied(dep_cache)
            except Exception:
                # Catchall exception handler to ensure progression even if Job
                # subclasses have bugs in their get_deps etc.
                log.error("Job %s: exception in dependency check: %s" % (job.id,
                                                                             '\n'.join(traceback.format_exception(*(sys.exc_info())))))
                self.complete_job(job, cancelled = True)
                continue

            if not deps_satisfied:
                log.warning("Job %d: cancelling because of failed dependency" % job.id)
                self.complete_job(job, cancelled = True)
                # TODO: tell someone WHICH dependency
                continue
            else:
                ok_jobs.append(job)

        return ok_jobs

    def _spawn_job(self, job):
        thread = RunJobThread(self, job)
        assert not job.id in self._run_threads
        self._run_threads[job.id] = thread
        thread.start()

    def _complete_job(self, job, errored, cancelled):
        if job.state == 'tasked':
            del self._run_threads[job.id]

        log.info("Job %s completing (errored=%s, cancelled=%s)" %
                     (job.id, errored, cancelled))
        self._job_collection.update(job, 'complete', errored = errored, cancelled = cancelled)

        try:
            command = Command.objects.filter(jobs = job, complete = False)[0]
        except IndexError:
            log.warning("Job %s: No incomplete command while completing" % job.pk)
            command = None

        locks = json.loads(job.locks_json)

        # Update _lock_cache to remove the completed job's locks
        self._lock_cache.remove_job(job)

        # Check for completion callbacks on anything this job held a writelock on
        for lock in locks:
            if lock['write']:
                lock = StateLock.from_dict(job, lock)
                log.debug("Job %s completing, held writelock on %s" % (job.pk, lock.locked_item))
                try:
                    self._completion_hooks(lock.locked_item, command)
                except Exception:
                    log.error("Error in completion hooks: %s" % '\n'.join(traceback.format_exception(*(sys.exc_info()))))

        for command in Command.objects.filter(jobs = job):
            command.check_completion()

    def _completion_hooks(self, changed_item, command = None):
        """
        :param command: If set, any created jobs are added
        to this command object.
        """
        if hasattr(changed_item, 'content_type'):
            changed_item = changed_item.downcast()

        log.debug("_completion_hooks command %s, %s (%s) state=%s" % (command, changed_item, changed_item.__class__, changed_item.state))

        def running_or_failed(klass, **kwargs):
            """Look for jobs of the same type with the same params, either incomplete (don't start the job because
            one is already pending) or complete in the same command (don't start the job because we already tried and failed)"""
            if command:
                count = klass.objects.filter(~Q(state = 'complete') | Q(command = command), **kwargs).count()
            else:
                count = klass.objects.filter(~Q(state = 'complete'), **kwargs).count()

            return bool(count)

        if isinstance(changed_item, FilesystemMember):
            fs = changed_item.filesystem
            members = list(ManagedMdt.objects.filter(filesystem = fs)) + list(ManagedOst.objects.filter(filesystem = fs))
            states = set([t.state for t in members])
            now = django.utils.timezone.now()

            if not fs.state == 'available' and changed_item.state in ['mounted', 'removed'] and states == set(['mounted']):
                log.debug('branched')
                self._notify_state(ContentType.objects.get_for_model(fs).natural_key(), fs.id, now, 'available', ['stopped', 'unavailable'])
            if changed_item.state == 'unmounted' and fs.state != 'stopped' and states == set(['unmounted']):
                self._notify_state(ContentType.objects.get_for_model(fs).natural_key(), fs.id, now, 'stopped', ['stopped', 'unavailable'])
            if changed_item.state == 'unmounted' and fs.state == 'available' and states != set(['mounted']):
                self._notify_state(ContentType.objects.get_for_model(fs).natural_key(), fs.id, now, 'unavailable', ['available'])

        if isinstance(changed_item, ManagedHost):
            if changed_item.state == 'lnet_up' and changed_item.lnetconfiguration.state != 'nids_known':
                if not running_or_failed(ConfigureLNetJob, lnet_configuration = changed_item.lnetconfiguration):
                    job = ConfigureLNetJob(lnet_configuration = changed_item.lnetconfiguration, old_state = 'nids_unknown')
                    if not command:
                        command = Command.objects.create(message = "Configuring LNet on %s" % changed_item)
                    CommandPlan(self._lock_cache, self._job_collection).add_jobs([job], command)
                else:
                    log.debug('running_or_failed')

            if changed_item.state == 'configured':
                if not running_or_failed(GetLNetStateJob, host = changed_item):
                    job = GetLNetStateJob(host = changed_item)
                    if not command:
                        command = Command.objects.create(message = "Getting LNet state for %s" % changed_item)
                    CommandPlan(self._lock_cache, self._job_collection).add_jobs([job], command)

        if isinstance(changed_item, ManagedTarget):
            if isinstance(changed_item, FilesystemMember):
                mgs = changed_item.filesystem.mgs
            else:
                mgs = changed_item

            if mgs.conf_param_version != mgs.conf_param_version_applied:
                if not running_or_failed(ApplyConfParams, mgs = mgs):
                    job = ApplyConfParams(mgs = mgs)
                    if DepCache().get(job).satisfied():
                        if not command:
                            command = Command.objects.create(message = "Updating configuration parameters on %s" % mgs)
                        CommandPlan(self._lock_cache, self._job_collection).add_jobs([job], command)

    @transaction.commit_on_success
    def complete_job(self, job, errored = False, cancelled = False):
        with self._lock:
            if job.state != 'tasked':
                # This happens if a Job is cancelled while it's calling this
                log.info("Job %s has state %s in complete_job" % (job.id, job.state))
                return

            ObjectCache.clear()

            self._complete_job(job, errored, cancelled)
            self._run_next()

    def set_state(self, object_ids, message, run):
        with self._lock:
            ObjectCache.clear()
            with transaction.commit_on_success():
                command = CommandPlan(self._lock_cache, self._job_collection).command_set_state(object_ids, message)
            self._job_collection.add_many(command.jobs.all())
            if run:
                self._run_next()
        return command.id

    def _notify_state(self, content_type, object_id, notification_time, new_state, from_states):
        # Get the StatefulObject
        from django.contrib.contenttypes.models import ContentType
        model_klass = ContentType.objects.get_by_natural_key(*content_type).model_class()
        instance = model_klass.objects.get(pk = object_id).downcast()

        # Assert its class
        from chroma_core.models import StatefulObject
        assert(isinstance(instance, StatefulObject))

        # If a state update is needed/possible
        if instance.state in from_states and instance.state != new_state:
            # Check that no incomplete jobs hold a lock on this object
            if not len(self._lock_cache.get_by_locked_item(instance)):
                modified_at = instance.state_modified_at
                modified_at = modified_at.replace(tzinfo = tz.tzutc())

                if notification_time > modified_at:
                    # No jobs lock this object, go ahead and update its state
                    log.info("notify_state: Updating state of item %s (%s) from %s to %s" % (instance.id, instance, instance.state, new_state))
                    instance.set_state(new_state)
                    ObjectCache.update(instance)

                    # FIXME: should check the new state against reverse dependencies
                    # and apply any fix_states
                    self._completion_hooks(instance)
                else:
                    log.info("notify_state: Dropping update of %s (%s) %s->%s because it has been updated since" % (instance.id, instance, instance.state, new_state))
                    pass
            else:
                log.info("notify_state: Dropping update to %s because of locks" % instance)
                for lock in self._lock_cache.get_by_locked_item(instance):
                    log.info("  %s" % lock)
        else:
            log.info("notify_state: Dropping update to %s because its state is %s" % (instance, instance.state))

    @transaction.commit_on_success
    def notify_state(self, content_type, object_id, time_serialized, new_state, from_states):
        with self._lock:
            ObjectCache.clear()

            notification_time = dateutil.parser.parse(time_serialized)
            self._notify_state(content_type, object_id, notification_time, new_state, from_states)

            self._run_next()

    @transaction.commit_on_success
    def run_jobs(self, job_dicts, message):
        with self._lock:
            ObjectCache.clear()

            result = CommandPlan(self._lock_cache, self._job_collection).command_run_jobs(job_dicts, message)
            self._run_next()
        return result

    @transaction.commit_on_success
    def cancel_job(self, job_id):
        with self._lock:
            try:
                job = self._job_collection.get(job_id)
            except KeyError:
                # Job has been cleaned out of collection, therefore is complete
                # However, to avoid being too trusting, let's retrieve it and
                # let the following check for completeness happen
                job = Job.objects.get(pk = job_id)

            log.info("cancel_job: Cancelling job %s (%s)" % (job.id, job.state))
            if job.state == 'complete':
                return
            elif job.state == 'tasked':
                try:
                    thread = self._run_threads[job_id]
                    thread.cancel()
                except KeyError:
                    pass
                self._job_collection.update(job, 'complete', cancelled = True)
            elif job.state == 'pending':
                self._job_collection.update(job, 'complete', cancelled = True)

            for command in Command.objects.filter(jobs = job_id):
                command.check_completion()

            # So that anything waiting on this job can be cancelled too
            self._run_next()
