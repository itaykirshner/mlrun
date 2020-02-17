# Copyright 2018 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import inspect
import json
import socket
from copy import deepcopy
from os import environ

import math
from kubernetes.client.rest import ApiException
from ..k8s_utils import get_k8s_helper
from ..utils import update_in, logger, normalize_name
from .base import FunctionStatus
from ..execution import MLClientCtx
from .local import get_func_arg, load_module, exec_from_params
from ..model import RunObject
from .kubejob import KubejobRuntime
from .pod import KubeResourceSpec
from ..lists import RunList
from ..config import config
from .utils import mlrun_key, get_resource_labels, get_func_selector, log_std, RunError


def get_dask_resource():
    return {
        'scope': 'function',
        'start': deploy_function,
        'list': list_objects,
        'status': get_obj_status,
        'clean': clean_objects,
    }


class DaskSpec(KubeResourceSpec):
    def __init__(self, command=None, args=None, image=None, mode=None,
                 volumes=None, volume_mounts=None, env=None, resources=None,
                 build=None, entry_points=None, description=None,
                 replicas=None, image_pull_policy=None, service_account=None,
                 image_pull_secret=None, extra_pip=None, remote=None,
                 service_type=None, nthreads=None,
                 node_port=None, min_replicas=None, max_replicas=None):

        super().__init__(command=command, args=args, image=image,
                         mode=mode, volumes=volumes, volume_mounts=volume_mounts,
                         env=env, resources=resources, replicas=replicas, image_pull_policy=image_pull_policy,
                         service_account=service_account, build=build,
                         entry_points=entry_points, description=description,
                         image_pull_secret=image_pull_secret)
        self.args = args

        self.extra_pip = extra_pip
        self.remote = remote
        if replicas or min_replicas or max_replicas:
            self.remote = True

        self.service_type = service_type
        self.node_port = node_port
        self.min_replicas = min_replicas or 0
        self.max_replicas = max_replicas or math.inf
        self.scheduler_timeout = '60 minutes'
        self.nthreads = nthreads or 1


class DaskStatus(FunctionStatus):
    def __init__(self, state=None, build_pod=None,
                 scheduler_address=None, cluster_name=None, node_ports=None):
        super().__init__(state, build_pod)

        self.scheduler_address = scheduler_address
        self.cluster_name = cluster_name
        self.node_ports = node_ports


class DaskCluster(KubejobRuntime):
    kind = 'dask'
    _is_nested = False
    _is_remote = False

    def __init__(self, spec=None,
                 metadata=None):
        super().__init__(spec, metadata)
        self._cluster = None
        self.spec.build.base_image = self.spec.build.base_image or 'daskdev/dask:latest'

    @property
    def spec(self) -> DaskSpec:
        return self._spec

    @spec.setter
    def spec(self, spec):
        self._spec = self._verify_dict(spec, 'spec', DaskSpec)

    @property
    def status(self) -> DaskStatus:
        return self._status

    @status.setter
    def status(self, status):
        self._status = self._verify_dict(status, 'status', DaskStatus)

    @property
    def is_deployed(self):
        if not self.spec.remote:
            return True
        return super().is_deployed

    @property
    def initialized(self):
        return True if self._cluster else False

    def _load_db_status(self):
        meta = self.metadata
        if self._is_remote_api():
            db = self._get_db()
            db_func = None
            try:
                db_func = db.get_function(meta.name, meta.project, meta.tag)
            except Exception:
                pass

            if db_func and 'status' in db_func:
                self.status = db_func['status']
                return 'scheduler_address' in db_func['status']

        return False

    def _start(self):
        if self._is_remote_api():
            db = self._get_db()
            if not self.is_deployed:
                raise RunError("function image is not built/ready, use .build()" 
                               " method first, or set base dask image (daskdev/dask:latest)")

            self.save(versioned=False)
            resp = db.remote_start(self._function_uri())
            if resp and 'status' in resp:
                self.status = resp['status']
            return

        self._cluster = deploy_function(self)
        self.save(versioned=False)

    def close(self, running=True):
        from dask.distributed import default_client
        try:
            client = default_client()
            client.close()
        except ValueError:
            pass

        # meta = self.metadata
        # s = get_func_selector(meta.project, meta.name, meta.tag)
        # clean_objects(s, running)

    def get_status(self):
        meta = self.metadata
        s = get_func_selector(meta.project, meta.name, meta.tag)
        if self._is_remote_api():
            db = self._get_db()
            return db.remote_status(self.kind, s)

        status = get_obj_status(s)
        print(status)
        return status

    def cluster(self):
        return self._cluster

    def _remote_addresses(self):
        if config.remote_host:
            if self.spec.service_type != 'NodePort':
                raise ValueError('remote host require NodePort')
            addr = '{}:{}'.format(config.remote_host,
                                  self.status.node_ports.get('scheduler'))
            dash = '{}:{}'.format(config.remote_host,
                                  self.status.node_ports.get('dashboard'))
            return addr, dash
        return self.status.scheduler_address, ''

    @property
    def client(self):
        from dask.distributed import Client, default_client

        if self.spec.remote and not self.status.scheduler_address:
            if not self._load_db_status():
                self._start()

        if self.status.scheduler_address:
            addr, dash = self._remote_addresses()
            try:
                client = Client(addr)
            except OSError as e:
                logger.warning('remote scheduler at {} not ready, will try to restart ()'.format(
                    addr, e
                ))

                # todo: figure out if test is needed
                # if self._is_remote_api():
                #     raise Exception('no access to Kubernetes API')

                status = self.get_status()
                if status != 'running':
                    self._start()
                addr = self.status.scheduler_address
                client = Client(addr)
            logger.info('using remote dask scheduler ({}) at: {}'.format(
                self.status.cluster_name, addr))
            if dash:
                logger.info('remote dashboard (node) port: {}'.format(
                    dash))

            return client
        try:
            return default_client()
        except ValueError:
            return Client()

    def deploy(self, watch=True, with_mlrun=False, skip_deployed=False):
        """deploy function, build container with dependencies"""
        return super().deploy(watch, with_mlrun, skip_deployed)

    def _run(self, runobj: RunObject, execution):

        handler = runobj.spec.handler
        self._force_handler(handler)

        environ['MLRUN_EXEC_CONFIG'] = runobj.to_json()
        if self.spec.rundb:
            environ['MLRUN_DBPATH'] = self.spec.rundb

        if not inspect.isfunction(handler):
            if not self.spec.command:
                raise ValueError('specified handler (string) without command '
                                 '(py file path), specify command or use handler pointer')
            mod, handler = load_module(self.spec.command, handler)
        context = MLClientCtx.from_dict(runobj.to_dict(),
                                        rundb=self.spec.rundb,
                                        autocommit=False,
                                        host=socket.gethostname())
        client = self.client
        setattr(context, 'dask_client', client)
        sout, serr = exec_from_params(handler, runobj, context)
        log_std(self._db_conn, runobj, sout, serr,
                skip=self.is_child, show=False)
        return context.to_dict()


def deploy_function(function: DaskCluster, secrets=None):
    try:
        from dask_kubernetes import KubeCluster, make_pod_spec
        from dask.distributed import Client, default_client
        from kubernetes_asyncio import client
        import dask
    except ImportError as e:
        print('missing dask or dask_kubernetes, please run '
              '"pip install dask distributed dask_kubernetes", %s', e)
        raise e

    spec = function.spec
    meta = function.metadata
    spec.remote = True

    image = function.full_image_path() or 'daskdev/dask:latest'
    env = spec.env
    namespace = meta.namespace or config.namespace
    if spec.extra_pip:
        env.append(spec.extra_pip)

    pod_labels = get_resource_labels(function)
    args = ['dask-worker', "--nthreads", str(spec.nthreads)]
    if spec.args:
        args += spec.args

    container = client.V1Container(name='base',
                                   image=image,
                                   env=env,
                                   args=args,
                                   image_pull_policy=spec.image_pull_policy,
                                   volume_mounts=spec.volume_mounts,
                                   resources=spec.resources)

    pod_spec = client.V1PodSpec(containers=[container],
                                restart_policy='Never',
                                volumes=spec.volumes,
                                service_account=spec.service_account)
    if spec.image_pull_secret:
        pod_spec.image_pull_secrets = [
            client.V1LocalObjectReference(name=spec.image_pull_secret)]

    pod = client.V1Pod(metadata=client.V1ObjectMeta(namespace=namespace,
                                                    labels=pod_labels),
                                                    #annotations=meta.annotation),
                       spec=pod_spec)

    svc_temp = dask.config.get("kubernetes.scheduler-service-template")
    if spec.service_type or spec.node_port:
        if spec.node_port:
            spec.service_type = 'NodePort'
            svc_temp['spec']['ports'][1]['nodePort'] = spec.node_port
        update_in(svc_temp, 'spec.type', spec.service_type)

    norm_name = normalize_name(meta.name)
    dask.config.set({"kubernetes.scheduler-service-template": svc_temp,
                     'kubernetes.name': 'mlrun-' + norm_name + '-{uuid}'})

    cluster = KubeCluster(
        pod, deploy_mode='remote',
        namespace=namespace,
        scheduler_timeout=spec.scheduler_timeout)

    logger.info('cluster {} started at {}'.format(
        cluster.name, cluster.scheduler_address
    ))

    function.status.scheduler_address = cluster.scheduler_address
    function.status.cluster_name = cluster.name
    if spec.service_type == 'NodePort':
        ports = cluster.scheduler.service.spec.ports
        function.status.node_ports = {'scheduler': ports[0].node_port,
                                      'dashboard': ports[1].node_port}

    if spec.replicas:
        cluster.scale(spec.replicas)
    else:
        cluster.adapt(minimum=spec.min_replicas,
                      maximum=spec.max_replicas)

    return cluster


def clean_objects(selector=[], running=False, namespace=None):
    k8s = get_k8s_helper()
    namespace = namespace or config.namespace

    selector = ','.join(['{}class=dask'.format(mlrun_key)] + selector)
    pods = k8s.v1api.list_namespaced_pod(namespace, label_selector=selector)
    service_names = []
    for pod in pods.items:
        status = pod.status.phase.lower()
        if running or status != 'running':
            comp = pod.metadata.labels.get('dask.org/component')
            if comp == 'scheduler':
                service_names.append(pod.metadata.labels.get('dask.org/cluster-name'))
            try:
                k8s.v1api.delete_namespaced_pod(pod.metadata.name, namespace)
                logger.info("Deleted pod: %s", pod.metadata.name)
            except ApiException as e:
                # ignore error if pod is already removed
                if e.status != 404:
                    raise

    services = k8s.v1api.list_namespaced_service(
        namespace, label_selector=selector
    )
    for service in services.items:
        try:
            if running or service.metadata.name in service_names:
                k8s.v1api.delete_namespaced_service(service.metadata.name, namespace)
                logger.info("Deleted service: %s", service.metadata.name)
        except ApiException as e:
            # ignore error if service is already removed
            if e.status != 404:
                raise


def get_obj_status(selector=[], namespace=None):
    k8s = get_k8s_helper()
    namespace = namespace or config.namespace
    selector = ','.join(['dask.org/component=scheduler'.format(mlrun_key)] + selector)
    pods = k8s.list_pods(namespace, selector=selector)
    status = ''
    for pod in pods:
        status = pod.status.phase.lower()
        print(pod)
        if status == 'running':
            cluster = pod.metadata.labels.get('dask.org/cluster-name')
            logger.info('found running dask function {}, cluster={}'.format(pod.metadata.name, cluster))
            return status
        logger.info('found dask function {} in non ready state ({})'.format(pod.metadata.name, status))
    return status


def list_objects(selector=[], namespace=None):
    k8s = get_k8s_helper()
    namespace = namespace or config.namespace
    selector = ','.join(['dask.org/component=scheduler'.format(mlrun_key)] + selector)
    pods = k8s.list_pods(namespace, selector=selector)
    objects = []
    for pod in pods:
        status = pod.status.phase.lower()
        objects.append([pod.metadata.name, status, pod.metadata.labels])

    return 'pod', objects


# def clean_objects(namespace=None, selector=[], states=None):
#     if not selector and not states:
#         raise ValueError(
#             'labels selector or states list must be specified')
#     items = list_objects(namespace, selector, states)
#     for item in items:
#         del_object(item.metadata.name, item.metadata.namespace)


