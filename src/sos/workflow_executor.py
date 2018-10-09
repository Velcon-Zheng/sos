#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import base64
import os
import subprocess
import sys
import time
import uuid
import zmq

from collections import defaultdict, Sequence
from io import StringIO
from typing import Any, Dict, List, Optional, Tuple, Union
from threading import Event

from ._version import __version__
from .dag import SoS_DAG, SoS_Node
from .eval import SoS_exec
from .hosts import Host
from .parser import SoS_Step, SoS_Workflow
from .pattern import extract_pattern
from .workflow_report import render_report
from .controller import Controller, connect_controllers, disconnect_controllers
from .section_analyzer import analyze_section
from .targets import (BaseTarget, RemovedTarget, UnavailableLock,
                      UnknownTarget, file_target, path, paths,
                      sos_step, sos_targets, sos_variable, textMD5)
from .utils import (Error, WorkflowDict, env, get_traceback,
                    load_config_files, pickleable, short_repr)
from .workers import SoS_Worker
from .executor_utils import __null_func__, PendingTasks

__all__ = []

try:
    # https://github.com/pytest-dev/pytest-cov/issues/139
    from pytest_cov.embed import cleanup_on_sigterm
    cleanup_on_sigterm()
except:
    pass


class ExecuteError(Error):
    """An exception to collect exceptions raised during run time so that
    other branches of the DAG would continue if some nodes fail to execute."""

    def __init__(self, workflow: str) -> None:
        Error.__init__(self)
        self.workflow = workflow
        self.errors = []
        self.traces = []
        self.args = (workflow, )

    def append(self, line: str, error: Exception) -> None:
        lines = [x for x in line.split('\n') if x.strip()]
        if not lines:
            short_line = '<empty>'
        else:
            short_line = lines[0][:40] if len(lines[0]) > 40 else lines[0]
        if short_line in self.errors:
            return
        self.errors.append(short_line)
        self.traces.append(get_traceback())
        newline = '\n' if self.message else ''
        self.message += f'{newline}[{short_line}]: {error}'



class dummy_node:
    # a dummy node object to store information of node passed
    # from nested workflow
    def __init__(self) -> None:
        pass


class ProcInfo(object):
    def __init__(self, worker: SoS_Worker, socket, step: Union[SoS_Node, dummy_node]) -> None:
        self.worker = worker
        self.socket = socket
        self.step = step

    def set_status(self, status: str) -> None:
        self.step._status = status

    def in_status(self, status: str) -> bool:
        return self.step._status == status

    def status(self):
        return self.step._status

    def is_pending(self) -> bool:
        return self.step._status.endswith('_pending')

class ExecutionManager(object):
    # this class managers workers and their status ...
    def __init__(self, max_workers: int, master: bool = True) -> None:
                #
        # running processes. It consisists of
        #
        # [ [proc, queue], socket, node]
        #
        # where:
        #   proc, queue: process, which is None for the nested workflow.
        #   socket: socket to get information from workers
        #   node: node that is being executed, which is a dummy node
        #       created on the fly for steps passed from nested workflow
        #
        self.procs = []

        # process pool that is used to pool temporarily unused processed.
        self.pool = []
        self.max_workers = max_workers

    def execute(self, runnable: Union[SoS_Node, dummy_node], config: Dict[str, Any], args: Any, spec: Any) -> None:
        if not self.pool:
            socket = env.zmq_context.socket(zmq.PAIR)
            port = socket.bind_to_random_port('tcp://127.0.0.1')
            worker = SoS_Worker(port=port, config=config, args=args)
            worker.start()
        else:
            # get worker, q and runnable is not needed any more
            pi = self.pool.pop(0)
            worker = pi.worker
            socket = pi.socket

        # we need to report number of active works, plus master process itself
        env.controller_push_socket.send_pyobj(['nprocs', self.num_active() + 1])

        socket.send_pyobj(spec)
        self.procs.append(ProcInfo(worker=worker, socket=socket, step=runnable))

    def add_placeholder_worker(self, runnable, socket):
        runnable._status = 'step_pending'
        self.procs.append(ProcInfo(worker=None, socket=socket, step=runnable))

    def num_active(self) -> int:
        return len([x for x in self.procs if x and not x.is_pending()
                 and not x.in_status('failed')])

    def all_busy(self) -> bool:
        return self.num_active() >= self.max_workers

    def all_done_or_failed(self) -> bool:
        return not self.procs or all(x.in_status('failed') for x in self.procs)

    def mark_idle(self, idx: int) -> None:
        self.pool.append(self.procs[idx])
        self.procs[idx] = None

    def cleanup(self) -> None:
        self.procs = [x for x in self.procs if x is not None]

    def terminate(self, brutal: bool = False) -> None:
        self.cleanup()
        if not brutal:
            for proc in self.procs + self.pool:
                proc.socket.send_pyobj(None)
                proc.socket.LINGER = 0
                proc.socket.close()
            time.sleep(0.1)
            for proc in self.procs + self.pool:
                if proc.worker and proc.worker.is_alive():
                    proc.worker.terminate()
                    proc.worker.join()
        else:
            for proc in self.procs + self.pool:
                # proc can be fake if from a nested workflow
                if proc.worker:
                    proc.worker.terminate()


class Base_Executor:
    '''This is the base class of all executor that provides common
    set up and tear functions for all executors.'''

    def __init__(self, workflow: Optional[SoS_Workflow] = None, args: Optional[Any] = None, shared: None = None,
                 config: Optional[Dict[str, Any]] = {}) -> None:
        self.workflow = workflow
        self.args = [] if args is None else args
        if '__args__' not in self.args:
            # if there is __args__, this is a nested workflow and we do not test this.
            for idx, arg in enumerate(self.args):
                wf_pars = self.workflow.parameters().keys()
                if isinstance(arg, str) and arg.startswith('--'):
                    if not wf_pars:
                        raise ValueError(
                            f'Undefined parameter {arg[2:]} for command line argument "{" ".join(args[idx:])}".')
                    pars = [arg[2:], arg[2:].replace('-', '_').split('=')[0]]
                    if arg[2:].startswith('no-'):
                        pars.extend(
                            [arg[5:], arg[5:].replace('-', '_').split('=')[0]])
                    if not any(x in wf_pars for x in pars):
                        raise ValueError(
                            f'Undefined parameter {arg[2:]} for command line argument "{" ".join(args[idx:])}". Acceptable parameters are: {", ".join(wf_pars)}')

        self.shared = {} if shared is None else shared
        env.config.update(config)
        if env.config['config_file'] is not None:
            env.config['config_file'] = os.path.abspath(
                os.path.expanduser(env.config['config_file']))
        #
        # if the executor is not called from command line, without sigmode setting
        if env.config['sig_mode'] is None:
            env.config['sig_mode'] = 'default'
        # interactive mode does not pass workflow
        self.md5 = self.calculate_md5()
        env.config['workflow_id'] = self.md5
        env.sos_dict.set('workflow_id', self.md5)

    def write_workflow_info(self):
        # if this is the outter most workflow, master)id should have =
        # not been set so we set it for all other workflows
        workflow_info = {
            'name': self.workflow.name,
            'start_time': time.time(),
        }
        # if this is the master ...
        if env.config['master_id'] == self.md5:
            workflow_info['command_line'] = subprocess.list2cmdline(
                [os.path.basename(sys.argv[0])] + sys.argv[1:])
            workflow_info['project_dir'] = os.getcwd()
            workflow_info['script'] = base64.b64encode(
                self.workflow.content.text().encode()).decode('ascii')
        workflow_info['master_id'] = env.config['master_id']
        if env.config['master_id'] == self.md5:
            env.signature_req_socket.send_pyobj(['workflow', 'clear'])
            env.signature_req_socket.recv_pyobj()
        env.signature_push_socket.send_pyobj(['workflow', 'workflow', self.md5, repr(workflow_info)])

    def handle_resumed(self):
        if env.config['resume_mode']:
            env.signature_req_socket.send_pyobj(['workflow_status', 'get', self.md5])
            status = env.signature_req_socket.recv_pyobj()
            if status:
                env.config['resumed_tasks'] = status['pending_tasks']
            else:
                env.logger.info(f'Workflow {self.md5} has been completed.')
                sys.exit(0)

    def run(self, targets: Optional[List[str]]=None, parent_socket: None=None, my_workflow_id: None=None, mode=None) -> Dict[str, Any]:
        #
        env.zmq_context = zmq.Context()

        if not env.config['master_id']:
            # if this is the executor for the master workflow, start controller
            env.config['master_id'] = self.md5
            #
            # control panel in a separate thread, connected by zmq socket
            if not parent_socket:
                ready = Event()
                self.controller = Controller(ready)
                self.controller.start()
                # wait for the thread to start with a signature_req saved to env.config
                ready.wait()
        if not parent_socket:
            connect_controllers(env.zmq_context)

        try:
            self.write_workflow_info()
            self.handle_resumed()

            # if this is a resumed task?
            if hasattr(env, 'accessed_vars'):
                delattr(env, 'accessed_vars')
            return self._run(targets=targets, parent_socket=parent_socket,
                my_workflow_id=my_workflow_id, mode=mode)
        finally:
            if not parent_socket and env.config['master_id'] == env.sos_dict['workflow_id']:
                # end progress bar when the master workflow stops
                env.logger.trace(f'Stop controller from {os.getpid()}')
                env.controller_req_socket.send_pyobj(['done'])
                env.controller_req_socket.recv()
                env.logger.trace('disconntecting master')
                disconnect_controllers(env.zmq_context)
                self.controller.join()
                # when the run() function is called again, the controller
                # thread will be start again.
                env.config['master_id'] = None


    def record_quit_status(self, tasks: List[Tuple[str, str]]) -> None:
        status_info = dict(env.config.items())
        queues = set([x[0] for x in tasks])
        status_info['pending_tasks'] = {x:[k[1] for k in tasks if k[0] == x] for x in queues}
        env.signature_push_socket.send_pyobj(['workflow_status', 'save', status_info])

    def calculate_md5(self) -> str:
        with StringIO() as sig:
            for step in self.workflow.sections + self.workflow.auxiliary_sections:
                sig.write(f'{step.step_name()}: {step.md5}\n')
            sig.write(f'{self.args}\n')
            return textMD5(sig.getvalue())[:16]

    def reset_dict(self) -> None:
        env.sos_dict = WorkflowDict()
        self.init_dict()

    def init_dict(self) -> None:
        env.parameter_vars.clear()

        env.sos_dict.set('workflow_id', self.md5)
        env.sos_dict.set('master_id', env.config['master_id'])
        env.sos_dict.set('__null_func__', __null_func__)
        env.sos_dict.set('__args__', self.args)
        # initial values
        env.sos_dict.set('SOS_VERSION', __version__)
        env.sos_dict.set('__step_output__', sos_targets([]))

        # load configuration files
        load_config_files(env.config['config_file'])

        SoS_exec('import os, sys, glob', None)
        SoS_exec('from sos.runtime import *', None)

        # excute global definition to get some basic setup
        try:
            SoS_exec(self.workflow.global_def)
        except Exception:
            if env.verbosity > 2:
                sys.stderr.write(get_traceback())
            raise

        env.sos_dict.quick_update(self.shared)

        if isinstance(self.args, dict):
            for key, value in self.args.items():
                if not key.startswith('__'):
                    env.sos_dict.set(key, value)

    def skip(self, section: SoS_Step) -> bool:
        if section.global_def:
            try:
                SoS_exec(section.global_def)
            except subprocess.CalledProcessError as e:
                raise RuntimeError(e.stderr)
            except RuntimeError as e:
                if env.verbosity > 2:
                    sys.stderr.write(get_traceback())
                raise RuntimeError(
                    f'Failed to execute statements\n"{section.global_def}"\n{e}')
        #
        if 'skip' in section.options:
            val_skip = section.options['skip']
            if val_skip is None or val_skip is True:
                env.logger.info(
                    f'``{section.step_name(True)}`` is ``ignored`` due to skip option.')
                return True
            elif val_skip is not False:
                raise RuntimeError(
                    f'The value of section option skip can only be None, True or False, {val_skip} provided')
        return False

    def match(self, target: BaseTarget, step: SoS_Step) -> Union[Dict[str, str], bool]:
        # for sos_step, we need to match step name
        if isinstance(target, sos_step):
            return step.match(target.target_name())
        if not 'provides' in step.options and 'autoprovides' not in step.options:
            return False
        patterns = step.options['provides'] if 'provides' in step.options else step.options['autoprovides']
        if isinstance(patterns, (str, BaseTarget, path)):
            patterns = [patterns]
        elif not isinstance(patterns, (sos_targets, Sequence, paths)):
            raise RuntimeError(
                f'Unknown target to match: {patterns} of type {patterns.__class__.__name__}')
        for p in patterns:
            # other targets has to match exactly
            if not isinstance(target, (str, file_target)) or \
                    not isinstance(p, (str, file_target)):
                if target == p:
                    return {}
                else:
                    continue

            # if this is a regular string
            res = extract_pattern(str(p), [str(target)])
            if res and not any(None in x for x in res.values()):
                return {x: y[0] for x, y in res.items()}
            # string match
            elif file_target(p) == target:
                return True
        return False

    def resolve_dangling_targets(self, dag: SoS_DAG, targets: Optional[sos_targets]=None) -> int:
        '''Feed dangling targets with their dependncies from auxiliary steps,
        optionally add other targets'''
        resolved = 0
        while True:
            added_node = 0
            dangling_targets, existing_targets = dag.dangling(targets)
            if dangling_targets:
                env.logger.debug(
                    f'Resolving {dangling_targets} objects from {dag.number_of_nodes()} nodes')
            # find matching steps
            # check auxiliary steps and see if any steps provides it
            for target in dangling_targets:
                # target might no longer be dangling after a section is added.
                if target not in dag.dangling(targets)[0]:
                    continue
                mo = [(x, self.match(target, x))
                      for x in self.workflow.auxiliary_sections]
                mo = [x for x in mo if x[1] is not False]
                if not mo:
                    #
                    # if no step produces the target, it is possible that it is an indexed step
                    # so the execution of its previous steps would solves the dependency
                    #
                    # find all the nodes that depends on target
                    nodes = dag._all_dependent_files[target]
                    for node in nodes:
                        # if this is an index step... simply let it depends on previous steps
                        if node._node_index is not None:
                            indexed = [x for x in dag.nodes() if x._node_index is not None and x._node_index <
                                       node._node_index and not x._output_targets.valid()]
                            indexed.sort(key=lambda x: x._node_index)
                            if not indexed:
                                raise RuntimeError(
                                    f'No step to generate target {target}{dag.steps_depending_on(target, self.workflow)}')
                            if isinstance(target, sos_step) and not any(self.workflow.section_by_id(x._step_uuid).match(target.target_name()) for x in indexed):
                                raise RuntimeError(
                                    f'No step to generate target {target}{dag.steps_depending_on(target, self.workflow)}')
                            # now, if it is not a sos_step, but its previous steps have already been executed and still
                            # could not satisfy the requirement..., we should generate an error
                            if not any(x._status is None or x._status.endswith('pending') for x in indexed):
                                # all previous status has been failed or completed...
                                raise RuntimeError(
                                    f'Previous step{" has" if len(indexed) == 1 else "s have"} not generated target {target}{dag.steps_depending_on(target, self.workflow)}')
                            if node._input_targets.valid():
                                node._input_targets = sos_targets([])
                            if node._depends_targets.valid():
                                node._depends_targets = sos_targets([])
                        else:
                            raise RuntimeError(
                                f'No step to generate target {target}{dag.steps_depending_on(target, self.workflow)}')
                    if nodes:
                        resolved += 1
                    continue
                if len(mo) > 1:
                    # sos_step('a') could match to step a_1, a_2, etc, in this case we are adding a subworkflow
                    if isinstance(target, sos_step):
                        # create a new forward_workflow that is different from the master one
                        dag.new_forward_workflow()
                        # get the step names
                        sections = sorted([x[0] for x in mo],
                                          key=lambda x: x.step_name())
                        # this is only useful for executing auxiliary steps and
                        # might interfere with the step analysis
                        env.sos_dict.pop('__default_output__', None)
                        #  no default input
                        default_input: sos_targets = sos_targets()
                        #
                        for idx, section in enumerate(sections):
                            if self.skip(section):
                                continue
                            res = analyze_section(section, default_input)

                            environ_vars = res['environ_vars']
                            signature_vars = res['signature_vars']
                            changed_vars = res['changed_vars']
                            # parameters, if used in the step, should be considered environmental
                            environ_vars |= env.parameter_vars & signature_vars

                            # add shared to targets
                            if res['changed_vars']:
                                if 'provides' in section.options:
                                    if isinstance(section.options['provides'], str):
                                        section.options.set(
                                            'provides', [section.options['provides']])
                                else:
                                    section.options.set('provides', [])
                                #
                                section.options.set('provides',
                                                    section.options['provides'] + [sos_variable(var) for var in changed_vars])

                            # build DAG with input and output files of step
                            env.logger.debug(
                                f'Adding step {res["step_name"]} with output {short_repr(res["step_output"])} to resolve target {target}')
                            context = {
                                '__signature_vars__': signature_vars,
                                '__environ_vars__': environ_vars,
                                '__changed_vars__': changed_vars,
                            }
                            if idx == 0:
                                context['__step_output__'] = env.sos_dict['__step_output__']
                            elif idx == len(sections) - 1:
                                # for the last step, we say the mini-subworkflow satisfies sos_step('a')
                                # we have to do it this way because by default the DAG only sees sos_step('a_1') etc
                                res['step_output'].extend(target)

                            node_name = section.step_name()
                            dag.add_step(section.uuid,
                                         node_name, idx,
                                         res['step_input'],
                                         res['step_depends'],
                                         res['step_output'],
                                         context=context)
                            default_input = res['step_output']
                        added_node += len(sections)
                        resolved += 1
                        # dag.show_nodes()
                        continue
                    else:
                        raise RuntimeError(
                            f'Multiple steps {", ".join(x[0].step_name() for x in mo)} to generate target {target}')
                #
                # only one step, we need to process it # execute section with specified input
                #
                # NOTE:  Auxiliary can be called with different output files and matching pattern
                # so we are actually creating a new section each time we need an auxillary step.
                #
                section = mo[0][0]
                if isinstance(mo[0][1], dict):
                    for k, v in mo[0][1].items():
                        env.sos_dict.set(k, v)
                #
                # for auxiliary, we need to set input and output, here
                # now, if the step does not provide any alternative (e.g. no variable generated
                # from patten), we should specify all output as output of step. Otherwise the
                # step will be created for multiple outputs. issue #243
                if mo[0][1]:
                    env.sos_dict['__default_output__'] = sos_targets(target)
                else:
                    env.sos_dict['__default_output__'] = sos_targets(
                        section.options['provides'])
                # will become input, set to None
                env.sos_dict['__step_output__'] = sos_targets()
                #
                res = analyze_section(section)
                if isinstance(target, sos_step) and target.target_name() != section.step_name():
                    # sos_step target "name" can be matched to "name_10" etc so we will have to
                    # ensure that the target is outputted from the "name_10" step.
                    # This has been done in a more advanced case when an entire workflow is
                    # added
                    res['step_output'].extend(target)
                #
                # build DAG with input and output files of step
                env.logger.debug(
                    f'Adding step {res["step_name"]} with output {short_repr(res["step_output"])} to resolve target {target}')
                if isinstance(mo[0][1], dict):
                    context = mo[0][1]
                else:
                    context = {}
                context['__signature_vars__'] = res['signature_vars']
                context['__environ_vars__'] = res['environ_vars']
                context['__changed_vars__'] = res['changed_vars']
                context['__default_output__'] = env.sos_dict['__default_output__']
                # NOTE: If a step is called multiple times with different targets, it is much better
                # to use different names because pydotplus can be very slow in handling graphs with nodes
                # with identical names.
                node_name = section.step_name()
                if env.sos_dict["__default_output__"]:
                    node_name += f' ({short_repr(env.sos_dict["__default_output__"])})'
                dag.add_step(section.uuid,
                             node_name, None,
                             res['step_input'],
                             res['step_depends'],
                             res['step_output'],
                             context=context)
                added_node += 1
                resolved += 1

            # for existing targets... we should check if it actually exists. If
            # not it would still need to be regenerated
            node_added = False
            existing_targets = set(dag.dangling(targets)[1])
            for target in existing_targets:
                if node_added:
                    existing_targets = set(dag.dangling(targets)[1])
                    node_added = False
                if target not in existing_targets:
                    continue
                if file_target(target).target_exists('target') if isinstance(target, str) else target.target_exists('target'):
                    continue
                mo = [(x, self.match(target, x))
                      for x in self.workflow.auxiliary_sections]
                mo = [x for x in mo if x[1] is not False]
                if not mo:
                    # this is ok, this is just an existing target, no one is designed to
                    # generate it.
                    continue
                if len(mo) > 1:
                    # this is not ok.
                    raise RuntimeError(
                        f'Multiple steps {", ".join(x[0].step_name() for x in mo)} to generate target {target}')
                #
                # only one step, we need to process it # execute section with specified input
                #
                section = mo[0][0]
                if isinstance(mo[0][1], dict):
                    for k, v in mo[0][1].items():
                        env.sos_dict.set(k, v)
                #
                # for auxiliary, we need to set input and output, here
                # now, if the step does not provide any alternative (e.g. no variable generated
                # from patten), we should specify all output as output of step. Otherwise the
                # step will be created for multiple outputs. issue #243
                if mo[0][1]:
                    env.sos_dict['__default_output__'] = sos_targets(target)
                else:
                    env.sos_dict['__default_output__'] = sos_targets(
                        section.options['provides'])
                # will become input, set to None
                env.sos_dict['__step_output__'] = sos_targets()
                #
                res = analyze_section(section)
                #
                # build DAG with input and output files of step
                env.logger.debug(
                    f'Adding step {res["step_name"]} with output {short_repr(res["step_output"])} to resolve target {target}')
                if isinstance(mo[0][1], dict):
                    context = mo[0][1]
                else:
                    context = {}
                context['__signature_vars__'] = res['signature_vars']
                context['__environ_vars__'] = res['environ_vars']
                context['__changed_vars__'] = res['changed_vars']
                context['__default_output__'] = env.sos_dict['__default_output__']
                # NOTE: If a step is called multiple times with different targets, it is much better
                # to use different names because pydotplus can be very slow in handling graphs with nodes
                # with identical names.
                node_name = section.step_name()
                if env.sos_dict["__default_output__"]:
                    node_name += f' {short_repr(env.sos_dict["__default_output__"])})'
                dag.add_step(section.uuid, node_name,
                             None, res['step_input'],
                             res['step_depends'], res['step_output'], context=context)
                node_added = True
                added_node += 1
                # this case do not count as resolved
                # resolved += 1
            if added_node == 0:
                break
        return resolved

    def initialize_dag(self, targets: Optional[List[str]] = [], nested: bool = False) -> SoS_DAG:
        '''Create a DAG by analyzing sections statically.'''
        self.reset_dict()

        dag = SoS_DAG(name=self.md5)
        default_input: sos_targets = sos_targets([])
        targets = sos_targets(targets)
        for idx, section in enumerate(self.workflow.sections):
            if self.skip(section):
                continue
            #
            res = analyze_section(section, default_input)

            environ_vars = res['environ_vars']
            signature_vars = res['signature_vars']
            changed_vars = res['changed_vars']
            # parameters, if used in the step, should be considered environmental
            environ_vars |= env.parameter_vars & signature_vars

            # add shared to targets
            if res['changed_vars']:
                if 'provides' in section.options:
                    if isinstance(section.options['provides'], str):
                        section.options.set(
                            'provides', [section.options['provides']])
                else:
                    section.options.set('provides', [])
                #
                section.options.set('provides',
                                    section.options['provides'] + [sos_variable(var) for var in changed_vars])

            context = {'__signature_vars__': signature_vars,
                       '__environ_vars__': environ_vars,
                       '__changed_vars__': changed_vars}

            # for nested workflow, the input is specified by sos_run, not None.
            if idx == 0:
                context['__step_output__'] = env.sos_dict['__step_output__']

            dag.add_step(section.uuid,
                         section.step_name(),
                         idx,
                         res['step_input'],
                         res['step_depends'],
                         res['step_output'],
                         context=context)
            default_input = res['step_output']
        #
        # analyze auxiliary steps
        for idx, section in enumerate(self.workflow.auxiliary_sections):
            res = analyze_section(section, default_input)
            environ_vars = res['environ_vars']
            signature_vars = res['signature_vars']
            changed_vars = res['changed_vars']
            # parameters, if used in the step, should be considered environmental
            environ_vars |= env.parameter_vars & signature_vars

            # add shared to targets
            if res['changed_vars']:
                if 'provides' in section.options:
                    if isinstance(section.options['provides'], str):
                        section.options.set(
                            'provides', [section.options['provides']])
                else:
                    section.options.set('provides', [])
                #
                section.options.set('provides',
                                    section.options['provides'] + [sos_variable(var) for var in changed_vars])
        #
        if self.resolve_dangling_targets(dag, targets) == 0:
            if targets:
                raise RuntimeError(
                    f'No step to generate target {targets}.')
        # now, there should be no dangling targets, let us connect nodes
        dag.build(self.workflow.auxiliary_sections)
        # dag.show_nodes()
        # trim the DAG if targets are specified
        if targets:
            dag = dag.subgraph_from(targets)
        # check error
        cycle = dag.circular_dependencies()
        if cycle:
            raise RuntimeError(
                f'Circular dependency detected {cycle}. It is likely a later step produces input of a previous step.')

        dag.save(env.config['output_dag'], init=not nested)
        return dag

    def describe_completed(self):
        # return a string to summarize completed and skipped steps, substeps, and tasks
        res = []
        # if '__subworkflow_completed__' in self.completed and self.completed['__subworkflow_completed__']:
        #    res.append(f"{self.completed['__subworkflow_completed__']} completed subworkflow{'s' if self.completed['__subworkflow_completed__'] > 1 else ''}")
        # if '__subworkflow_skipped__' in self.completed and self.completed['__subworkflow_skipped__']:
        #    res.append(f"{self.completed['__subworkflow_skipped__']} skipped subworkflow{'s' if self.completed['__subworkflow_skipped__'] > 1 else ''}")
        if '__step_completed__' in self.completed and self.completed['__step_completed__']:
            res.append(
                f"{round(self.completed['__step_completed__'], 1)} completed step{'s' if self.completed['__step_completed__'] > 1 else ''}")
            if '__substep_completed__' in self.completed and self.completed['__substep_completed__'] and self.completed['__substep_completed__'] != self.completed['__step_completed__']:
                res.append(
                    f"{self.completed['__substep_completed__']} completed substep{'s' if self.completed['__substep_completed__'] > 1 else ''}")
        if '__step_skipped__' in self.completed and self.completed['__step_skipped__']:
            res.append(
                f"{round(self.completed['__step_skipped__'], 1)} ignored step{'s' if self.completed['__step_skipped__'] > 1 else ''}")
            if '__substep_skipped__' in self.completed and self.completed['__substep_skipped__'] and self.completed['__substep_skipped__'] != self.completed['__step_skipped__']:
                res.append(
                    f"{self.completed['__substep_skipped__']} ignored substep{'s' if self.completed['__substep_skipped__'] > 1 else ''}")
        if '__task_completed__' in self.completed and self.completed['__task_completed__']:
            res.append(
                f"{self.completed['__task_completed__']} completed task{'s' if self.completed['__task_completed__'] > 1 else ''}")
        if '__task_skipped__' in self.completed and self.completed['__task_skipped__']:
            res.append(
                f"{self.completed['__task_skipped__']} ignored task{'s' if self.completed['__task_skipped__'] > 1 else ''}")
        if len(res) > 1:
            return ', '.join(res[:-1]) + ' and ' + res[-1]
        elif len(res) == 1:
            return res[0]
        else:
            return 'no step executed'

    def check_targets(self, targets: sos_targets):
        for target in sos_targets(targets):
            if target.target_exists('target'):
                if env.config['sig_mode'] == 'force':
                    env.logger.info(f'Re-generating {target}')
                    target.unlink()
                else:
                    env.logger.info(f'Target {target} already exists')
        return sos_targets([x for x in targets if not file_target(x).target_exists('target') or env.config['sig_mode'] == 'force'])

    def step_completed(self, res, dag, runnable):
        for k, v in res['__completed__'].items():
            self.completed[k] += v
        # if the result of the result of a step
        svar = {}
        for k, v in res.items():
            if k == '__shared__':
                svar = v
                env.sos_dict.update(v)
            else:
                env.sos_dict.set(k, v)
        #
        # set context to the next logic step.
        for edge in dag.out_edges(runnable):
            node = edge[1]
            # if node is the logical next step...
            if node._node_index is not None and runnable._node_index is not None:
                # and node._node_index == runnable._node_index + 1:
                node._context.update(env.sos_dict.clone_selected_vars(
                    node._context['__signature_vars__'] | node._context['__environ_vars__']
                    | {'_input', '__step_output__', '__args__'}))
            node._context.update(svar)
        dag.update_step(runnable,
                        env.sos_dict['__step_input__'],
                        env.sos_dict['__step_output__'],
                        env.sos_dict['__step_depends__'])
        runnable._status = 'completed'
        dag.save(env.config['output_dag'])

    def handle_unknown_target(self, res, dag, runnable):
        runnable._status = None
        dag.save(env.config['output_dag'])
        target = res.target

        if dag.regenerate_target(target):
            # runnable._depends_targets.append(target)
            # dag._all_dependent_files[target].append(runnable)
            dag.build(self.workflow.auxiliary_sections)
            #
            cycle = dag.circular_dependencies()
            if cycle:
                raise RuntimeError(
                    f'Circular dependency detected {cycle} after regeneration. It is likely a later step produces input of a previous step.')

        else:
            if self.resolve_dangling_targets(dag, sos_targets(target)) == 0:
                raise RuntimeError(
                    f'Failed to regenerate or resolve {target}{dag.steps_depending_on(target, self.workflow)}.')
            if runnable._depends_targets.valid():
                runnable._depends_targets.extend(target)
            if runnable not in dag._all_dependent_files[target]:
                dag._all_dependent_files[target].append(
                    runnable)
            dag.build(self.workflow.auxiliary_sections)
            #
            cycle = dag.circular_dependencies()
            if cycle:
                raise RuntimeError(
                    f'Circular dependency detected {cycle}. It is likely a later step produces input of a previous step.')
        dag.save(env.config['output_dag'])

    def handle_unavailable_lock(self, res, dag, runnable):
        runnable._status = 'signature_pending'
        dag.save(env.config['output_dag'])
        runnable._signature = (res.output, res.sig_file)
        section = self.workflow.section_by_id(
            runnable._step_uuid)
        env.logger.info(
            f'Waiting on another process for step {section.step_name(True)}')

    def finalize_and_report(self):
        # remove task pending status if the workflow is completed normally
        env.signature_push_socket.send_pyobj(['workflow_status', 'completed'])
        if self.workflow.name != 'scratch':
            if self.completed["__step_completed__"] == 0:
                sts = 'ignored'
            elif env.config["run_mode"] == 'dryrun':
                sts = 'tested successfully'
            else:
                sts = 'executed successfully'
            env.logger.info(
                f'Workflow {self.workflow.name} (ID={self.md5}) is {sts} with {self.describe_completed()}.')
        if env.config['output_dag']:
            env.logger.info(
                f"Workflow DAG saved to {env.config['output_dag']}")
        workflow_info = {
            'end_time': time.time(),
            'stat': dict(self.completed),
        }
        if env.config['output_dag'] and env.config['master_id'] == self.md5:
            workflow_info['dag'] = env.config['output_dag']
        env.signature_push_socket.send_pyobj(['workflow', 'workflow', self.md5, repr(workflow_info)])
        if env.config['master_id'] == env.config['workflow_id'] and env.config['output_report']:
            # if this is the outter most workflow
            render_report(env.config['output_report'],
                          env.sos_dict['workflow_id'])
        if env.config['run_mode'] == 'dryrun':
            env.signature_req_socket.send_pyobj(['workflow', 'placeholders', env.sos_dict['workflow_id']])
            for filename in env.signature_req_socket.recv_pyobj():
                try:
                    if os.path.getsize(file_target(filename)) == 0:
                        file_target(filename).unlink()
                        env.logger.debug(f'Remove placeholder {filename}')
                except Exception as e:
                    env.logger.trace(f'Failed to remove placeholder {filename}: {e}')

    def _run(self, targets: Optional[List[str]]=None, parent_socket: None=None, my_workflow_id: None=None, mode=None) -> Dict[str, Any]:
        '''Execute a workflow with specified command line args. If sub is True, this
        workflow is a nested workflow and be treated slightly differently.
        '''
        #
        # There are threee cases
        #
        # parent_socket = None: this is the master workflow executor
        # parent_socket != None, my_workflow_id != None: this is a nested workflow inside a master workflow
        #   executor and needs to pass tasks etc to master
        # parent_socket != None, my_workflow_id == None: this is a nested workflow inside a task and needs to
        #   handle its own tasks.
        #
        nested = parent_socket is not None and my_workflow_id is not None
        self.completed = defaultdict(int)

        def i_am():
            return 'Nested' if nested else 'Master'

        self.reset_dict()
        env.config['run_mode'] = env.config.get(
            'run_mode', 'run') if mode is None else mode
        # passing run_mode to SoS dict so that users can execute blocks of
        # python statements in different run modes.
        env.sos_dict.set('run_mode', env.config['run_mode'])

        wf_result = {'__workflow_id__': my_workflow_id, 'shared': {}}
        # if targets are specified and there are only signatures for them, we need
        # to remove the signature and really generate them
        if targets:
            targets = self.check_targets(targets)
            if len(targets) == 0:
                if parent_socket:
                    parent_socket.send_pyobj(wf_result)
                else:
                    return wf_result

        # process step of the pipelinp
        dag = self.initialize_dag(targets=targets, nested=nested)
        # process step of the pipelinp
        #
        # running processes. It consisists of
        #
        # [ [proc, queue], socket, node]
        #
        # where:
        #   proc, queue: process, which is None for the nested workflow.
        #   socket: socket to get information from workers
        #   node: node that is being executed, which is a dummy node
        #       created on the fly for steps passed from nested workflow
        #
        manager = ExecutionManager(
            env.config['max_procs'], master=not nested)
        #
        # steps sent and queued from the nested workflow
        # they will be executed in random but at a higher priority than the steps
        # on the master process.
        self.step_queue = {}
        try:
            exec_error = ExecuteError(self.workflow.name)
            while True:
                # step 1: check existing jobs and see if they are completed
                for idx, proc in enumerate(manager.procs):
                    if proc is None:
                        continue

                    runnable = proc.step
                    # echck if there is any message from the socket
                    if not proc.socket.poll(0.001):
                        continue

                    # receieve something from the pipe
                    res = proc.socket.recv_pyobj()
                    #
                    # if this is NOT a result, rather some request for task, step, workflow etc
                    if isinstance(res, str):
                        if nested:
                            raise RuntimeError(
                                f'Nested workflow is not supposed to receive task, workflow, or step requests. {res} received.')
                        if res.startswith('task'):
                            env.logger.debug(
                                f'{i_am()} receives task request {res}')
                            host = res.split(' ')[1]
                            if host == '__default__':
                                if 'default_queue' in env.config:
                                    host = env.config['default_queue']
                                else:
                                    host = 'localhost'
                            try:
                                new_tasks = res.split(' ')[2:]
                                runnable._host = Host(host)
                                if hasattr(runnable, '_pending_tasks'):
                                    runnable._pending_tasks.extend(new_tasks)
                                else:
                                    runnable._pending_tasks = new_tasks
                                for task in new_tasks:
                                    runnable._host.submit_task(task)
                                runnable._status = 'task_pending'
                                dag.save(env.config['output_dag'])
                                env.logger.trace('Step becomes task_pending')
                            except Exception as e:
                                proc.socket.send_pyobj(
                                    {x: {'ret_code': 1, 'task': x, 'output': {}, 'exception': e} for x in new_tasks})
                                proc.set_status('failed')
                            continue
                        elif res.startswith('step'):
                            # step sent from nested workflow
                            step_id = res.split(' ')[1]
                            step_params = proc.socket.recv_pyobj()
                            env.logger.debug(
                                f'{i_am()} receives step request {step_id} with args {step_params[3]}')
                            self.step_queue[step_id] = step_params
                            continue
                        elif res.startswith('workflow'):
                            workflow_id = res.split(' ')[1]
                            # receive the real definition
                            env.logger.debug(
                                f'{i_am()} receives workflow request {workflow_id}')
                            # (wf, args, shared, config)
                            wf, targets, args, shared, config = proc.socket.recv_pyobj()
                            # a workflow needs to be executed immediately because otherwise if all workflows
                            # occupies all workers, no real step could be executed.

                            # now we would like to find a worker and
                            runnable._pending_workflow = workflow_id
                            runnable._status = 'workflow_pending'
                            dag.save(env.config['output_dag'])

                            wfrunnable = dummy_node()
                            wfrunnable._node_id = workflow_id
                            wfrunnable._status = 'workflow_running_pending'
                            dag.save(env.config['output_dag'])
                            wfrunnable._pending_workflow = workflow_id
                            #
                            manager.execute(wfrunnable, config=config, args=args,
                                            spec=('workflow', workflow_id, wf, targets, args, shared, config))
                            #
                            continue
                        else:
                            raise RuntimeError(
                                f'Unexpected value from step {short_repr(res)}')

                    # if we does get the result, we send the process to pool
                    manager.mark_idle(idx)

                    env.logger.debug(
                        f'{i_am()} receive a result {short_repr(res)}')
                    if hasattr(runnable, '_from_nested'):
                        # if the runnable is from nested, we will need to send the result back to the workflow
                        env.logger.debug(f'{i_am()} send res to nested')
                        runnable._status = 'completed'
                        dag.save(env.config['output_dag'])
                        runnable._child_socket.send_pyobj(res)
                        # this is a onetime use socket that passes results from
                        # nested workflow to master
                        runnable._child_socket.LINGER = 0
                        runnable._child_socket.close()
                    elif isinstance(res, (UnknownTarget, RemovedTarget)):
                        self.handle_unknown_target(res, dag, runnable)
                    elif isinstance(res, UnavailableLock):
                        self.handle_unavailable_lock(res, dag, runnable)

                    # if the job is failed
                    elif isinstance(res, Exception):
                        env.logger.debug(f'{i_am()} received an exception')
                        runnable._status = 'failed'
                        dag.save(env.config['output_dag'])
                        exec_error.append(runnable._node_id, res)
                        # if this is a node for a running workflow, need to mark it as failed as well
                        #                        for proc in procs:
                        if isinstance(runnable, dummy_node) and hasattr(runnable, '_pending_workflow'):
                            for proc in manager.procs:
                                if proc is None:
                                    continue
                                if proc.is_pending() and hasattr(proc.step, '_pending_workflow') \
                                        and proc.step._pending_workflow == runnable._pending_workflow:
                                    proc.set_status('failed')
                            dag.save(env.config['output_dag'])
                    elif '__step_name__' in res:
                        env.logger.debug(f'{i_am()} receive step result ')
                        self.step_completed(res, dag, runnable)
                    elif '__workflow_id__' in res:
                        # result from a workflow
                        # the worker process has been returned to the pool, now we need to
                        # notify the step that is waiting for the result
                        env.logger.debug(f'{i_am()} receive workflow result')
                        # aggregate steps etc with subworkflows
                        for k, v in res['__completed__'].items():
                            self.completed[k] += v
                        # if res['__completed__']['__step_completed__'] == 0:
                        #    self.completed['__subworkflow_skipped__'] += 1
                        # else:
                        #    self.completed['__subworkflow_completed__'] += 1
                        for proc in manager.procs:
                            if proc is None:
                                continue
                            if proc.in_status('workflow_pending') and proc.step._pending_workflow == res['__workflow_id__']:
                                proc.socket.send_pyobj(res)
                                proc.set_status('running')
                                break
                        dag.save(env.config['output_dag'])
                    else:
                        raise RuntimeError(
                            f'Unrecognized response from a step: {res}')

                # remove None
                manager.cleanup()

                # step 2: check if some jobs are done
                for proc_idx, proc in enumerate(manager.procs):
                    # if a job is pending, check if it is done.
                    if proc.in_status('task_pending'):
                        res = proc.step._host.check_status(
                            proc.step._pending_tasks)
                        # env.logger.warning(res)
                        if any(x in ('aborted', 'failed') for x in res):
                            for t, s in zip(proc.step._pending_tasks, res):
                                if s in ('aborted', 'failed') and not (hasattr(proc.step, '_killed_tasks') and t in proc.step._killed_tasks):
                                    # env.logger.warning(f'{t} ``{s}``')
                                    if not hasattr(proc.step, '_killed_tasks'):
                                        proc.step._killed_tasks = {t}
                                    else:
                                        proc.step._killed_tasks.add(t)
                            if all(x in ('completed', 'aborted', 'failed') for x in res):
                                # we try to get results
                                task_status = proc.step._host.retrieve_results(
                                    proc.step._pending_tasks)
                                proc.socket.send_pyobj(task_status)
                                proc.set_status('failed')
                                status = [('completed', len([x for x in res if x == 'completed'])),
                                          ('failed', len(
                                              [x for x in res if x == 'failed'])),
                                          ('aborted', len(
                                              [x for x in res if x == 'aborted']))
                                          ]
                                raise RuntimeError(
                                    ', '.join([f'{y} job{"s" if y > 1 else ""} {x}' for x, y in status if y > 0]))
                        if any(x in ('new', 'pending', 'submitted', 'running') for x in res):
                            continue
                        elif all(x == 'completed' for x in res):
                            env.logger.debug(
                                f'Proc {proc_idx} puts results for {" ".join(proc.step._pending_tasks)} from step {proc.step._node_id}')
                            res = proc.step._host.retrieve_results(
                                proc.step._pending_tasks)
                            proc.socket.send_pyobj(res)
                            proc.step._pending_tasks = []
                            proc.set_status('running')
                        else:
                            raise RuntimeError(
                                f'Job returned with status {res}')

                # step 3: check if there is room and need for another job
                while True:
                    if manager.all_busy():
                        break
                    #
                    # if steps from child nested workflow?
                    if self.step_queue:
                        step_id, step_param = self.step_queue.popitem()
                        section, context, shared, args, config, verbosity, port = step_param
                        # run it!
                        runnable = dummy_node()
                        runnable._node_id = step_id
                        runnable._status = 'running'
                        dag.save(env.config['output_dag'])
                        runnable._from_nested = True
                        runnable._child_socket = env.zmq_context.socket(zmq.PAIR)
                        runnable._child_socket.connect(f'tcp://127.0.0.1:{port}')

                        env.logger.debug(
                            f'{i_am()} sends {section.step_name()} from step queue with args {args} and context {context}')

                        manager.execute(runnable, config=env.config, args=self.args,
                                        spec=('step', section, context, shared, args, config, verbosity))
                        continue

                    # find any step that can be executed and run it, and update the DAT
                    # with status.
                    runnable = dag.find_executable()
                    if runnable is None:
                        # no runnable
                        # dag.show_nodes()
                        break

                    # find the section from runnable
                    section = self.workflow.section_by_id(runnable._step_uuid)
                    # execute section with specified input
                    runnable._status = 'running'
                    dag.save(env.config['output_dag'])

                    # workflow shared variables
                    shared = {x: env.sos_dict[x] for x in self.shared.keys(
                    ) if x in env.sos_dict and pickleable(env.sos_dict[x], x)}
                    if 'shared' in section.options:
                        if isinstance(section.options['shared'], str):
                            svars = [section.options['shared']]
                        elif isinstance(section.options['shared'], dict):
                            svars = section.options['shared'].keys()
                        elif isinstance(section.options['shared'], Sequence):
                            svars = []
                            for x in section.options['shared']:
                                if isinstance(x, str):
                                    svars.append(x)
                                elif isinstance(x, dict):
                                    svars.extend(x.keys())
                                else:
                                    raise ValueError(
                                        f'Unacceptable value for parameter shared: {section.options["shared"]}')
                        else:
                            raise ValueError(
                                f'Unacceptable value for parameter shared: {section.options["shared"]}')
                        shared.update(
                            {x: env.sos_dict[x] for x in svars if x in env.sos_dict and pickleable(env.sos_dict[x], x)})

                    if 'workflow_id' in env.sos_dict:
                        runnable._context['workflow_id'] = env.sos_dict['workflow_id']

                    if not nested:
                        env.logger.debug(
                            f'{i_am()} execute {section.md5} from DAG')
                        manager.execute(runnable, config=env.config, args=self.args,
                                        spec=('step', section, runnable._context, shared, self.args,
                                              env.config, env.verbosity))
                    else:
                        # send the step to the parent
                        step_id = uuid.uuid4()
                        env.logger.debug(
                            f'{i_am()} send step {section.step_name()} to master with args {self.args} and context {runnable._context}')
                        parent_socket.send_pyobj(f'step {step_id}')

                        socket = env.zmq_context.socket(zmq.PAIR)
                        port = socket.bind_to_random_port('tcp://127.0.0.1')
                        parent_socket.send_pyobj((section, runnable._context, shared, self.args,
                                          env.config, env.verbosity, port))
                        # this is a real step
                        manager.add_placeholder_worker(runnable, socket)

                if manager.all_done_or_failed():
                    break

                # if -W is specified, or all task queues are not wait
                elif all(x.in_status('task_pending') for x in manager.procs) and \
                        (env.config['wait_for_task'] is False or
                         (env.config['wait_for_task'] is None and Host.not_wait_for_tasks())):
                    # if all jobs are pending, let us check if all jbos have been submitted.
                    pending_tasks = []
                    running_tasks = []
                    for n in [x.step for x in manager.procs]:
                        p, r = n._host._task_engine.get_tasks()
                        pending_tasks.extend(p)
                        running_tasks.extend([(n._host.alias, x) for x in r])
                    if not pending_tasks and running_tasks:
                        env.logger.trace(
                            f'Exit with {len(running_tasks)} running tasks: {running_tasks}')
                        raise PendingTasks(running_tasks)
                else:
                    time.sleep(0.1)
        except KeyboardInterrupt:
            if exec_error.errors:
                failed_steps, pending_steps = dag.pending()
                if pending_steps:
                    sections = [self.workflow.section_by_id(
                        x._step_uuid).step_name() for x in pending_steps]
                    exec_error.append(self.workflow.name,
                                      RuntimeError(
                                          f'{len(sections)} pending step{"s" if len(sections) > 1 else ""}: {", ".join(sections)}'))
                    raise exec_error
            else:
                raise
        except PendingTasks as e:
            self.record_quit_status(e.tasks)
            wf_result['pending_tasks'] = [x[1] for x in running_tasks]
            env.logger.info(
                f'Workflow {self.workflow.name} (ID={self.md5}) exits with {len(e.tasks)} running tasks')
            for task in e.tasks:
                env.logger.info(task[1])
            # close all processes
        except Exception as e:
            manager.terminate(brutal=True)
            raise e
        finally:
            if not nested:
                manager.terminate()
        #

        if exec_error.errors:
            failed_steps, pending_steps = dag.pending()
            # if failed_steps:
            # sections = [self.workflow.section_by_id(x._step_uuid).step_name() for x in failed_steps]
            # exec_error.append(self.workflow.name,
            #    RuntimeError('{} failed step{}: {}'.format(len(sections),
            #        's' if len(sections) > 1 else '', ', '.join(sections))))
            if pending_steps:
                sections = [self.workflow.section_by_id(
                    x._step_uuid).step_name() for x in pending_steps]
                exec_error.append(self.workflow.name,
                                  RuntimeError(
                                      f'{len(sections)} pending step{"s" if len(sections) > 1 else ""}: {", ".join(sections)}'))
            if parent_socket is not None:
                parent_socket.send_pyobj(exec_error)
                return wf_result
            else:
                raise exec_error
        elif 'pending_tasks' not in wf_result or not wf_result['pending_tasks']:
            self.finalize_and_report()
        else:
            # exit with pending tasks
            pass
        wf_result['shared'] = {x: env.sos_dict[x]
                               for x in self.shared.keys() if x in env.sos_dict}
        wf_result['__completed__'] = self.completed
        if parent_socket:
            parent_socket.send_pyobj(wf_result)
        else:
            return wf_result
