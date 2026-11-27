"""
A module for listing and creating Argo Workflows with Python.

Notes for maintainers:

1. Some of the constructors in the upstream argo_workflow_tools package appear to be
broken. I workaround this by calling the model constructor, then assigning any
properties that were not properly assigned by the constructor. This works fine, but
it leads to unusual code in some places where some arguments are passed to the
constructor, while others are assigned as instance properties immediately after the
constructor is called. E.g.

```python
# What should work correctly
metadata = v1_1.ObjectMeta(
    namespace=environ().ARGO_NAMESPACE,
    generate_name = environ().ARGO_TEMPLATE_NAME,
    ,)

# What I do instead to workaround the upstream issue
metadata = v1_1.ObjectMeta(namespace=environ().ARGO_NAMESPACE,)
metadata.generate_name = generate_name = environ().ARGO_TEMPLATE_NAME
```
"""
import itertools
import requests
import warnings
from typing import Dict, List, Optional
from pydantic import SecretStr

from argo_workflows import (
    ApiClient,
    #WorkflowServiceApi,
    Configuration,
    #V1alpha1WorkflowCreateRequest,
)

from argo_workflows.api.workflow_service_api import WorkflowServiceApi
from argo_workflows.models import IoArgoprojWorkflowV1alpha1WorkflowCreateRequest

from modules.utils.argo_workflow_tools_local import api_core_v1 as v1
from modules.utils.argo_workflow_tools_local import api_meta_v1 as v1_1

from modules.utils.argo_workflow_tools_local.io_v1alpha1 import (
    Arguments,
    DagTemplate,
    DagTask,
    Item,
    Metadata,
    Parameter,
    PodGc,
    WorkflowSpec,
    Template,
    TemplateRef,
    TtlStrategy,
    Workflow,
    WorkflowStatus,
)
from modules.utils.argo_helpers import pipeline_template_instances

from config.api import DataSource
from config.env import environ
from config.argo import (
    ArgoConfig,
    PipelineConfigs,
    fc_composite_pipelines,
    ArgoStatusCodes,
    FrontierCountsStatusCodes,
    STATUS_CODE_MAP,
)

def get_fresh_token(path) -> SecretStr:
    """ Loads the contents of JWT from the path to mounted JWT.
        Due to the Kubernetes update 1.30, JWT expires every one hour.
        This Function fetches the token dynamically before each request.
        

        Args:
            path (str): Path to JWT for service account.

        Returns:
            SecretStr: The wrapped contents of the mounted JWT.
    """
    with open(path, "r") as f:
        return SecretStr(f.read().strip())

def argo_dag_task(
    template_ref_name: str,
    task_name: str,
    depends: str,
    script_name: str,
    args: str,
    with_items: Optional[List[Item]] = None,
) -> DagTask:
    """Generates Pydantic model for a DAG Task.

    Args:
        template_ref_name (str): name property of TemplateRef
        script_name (str): name of the CLI script to run
        args (str): string containing args passed to the script
    """
    dag_task = DagTask(
        name=task_name,
        depends=depends,
        arguments=Arguments(
            parameters=[
                Parameter(
                    name="script_name",
                    value=script_name,
                ),
                Parameter(
                    name="args",
                    value=args,
                ),
            ]
        ),
    )
    # Attribute assignment isn't validated in pydantic v2, so wrap raw values
    # (e.g. dicts) in `Item` explicitly to keep serialization type-correct.
    dag_task.with_items = (
        [i if isinstance(i, Item) else Item(i) for i in with_items]
        if with_items is not None
        else None
    )
    template_ref = TemplateRef(name=template_ref_name)
    template_ref.template = "main"
    dag_task.template_ref = template_ref
    return dag_task


def argo_metadata() -> v1_1.ObjectMeta:
    metadata = v1_1.ObjectMeta(
        namespace=environ().ARGO_NAMESPACE,
    )
    metadata.generate_name = environ().ARGO_TEMPLATE_NAME
    return metadata


def argo_spec(template: Template) -> WorkflowSpec:
    spec = WorkflowSpec()
    spec.service_account_name = environ().ARGO_SERVICE_ACCOUNT
    spec.pod_metadata = Metadata(
        annotations=ArgoConfig().pod_metadata_annotations,
    )
    spec.image_pull_secrets = [
        v1.LocalObjectReference(
            name=secret,
        )
        for secret in ArgoConfig().image_pull_secrets
    ]
    spec.entrypoint = environ().ARGO_TEMPLATE_ENTRYPOINT
    spec.pod_gc = PodGc(strategy="OnPodCompletion")
    spec.pod_priority_class_name = "business-value-medium"
    spec.ttl_strategy = TtlStrategy(seconds_after_completion=2592000)
    spec.templates = [template]
    return spec


def argo_single_template(cli_script: str, cmd: List[str], pipeline_name: str):
    workflow_template_name = environ().ARGO_TEMPLATE_NAME
    tasks = [
        argo_dag_task(
            workflow_template_name,
            "single-pipeline-workflow",
            None,
            cli_script,
            " ".join(cmd),
        )
    ]
    name = environ().ARGO_TEMPLATE_ENTRYPOINT
    dag = DagTemplate(tasks=tasks)
    metadata = Metadata(labels={"pipelineName": pipeline_name})
    single_template = Template(name=name)
    single_template.metadata = metadata
    single_template.dag = dag
    return single_template


def argo_ndm_with_items_template(
    num_periods: str, date: str, with_items: Dict[str, List]
):
    composite_pipeline_config = fc_composite_pipelines(PipelineConfigs.ndm_pipelines)[
        "ndm_tables_to_outbox"
    ]
    ndm_24100051 = composite_pipeline_config[0]
    ndm_24100054 = composite_pipeline_config[1]
    ndm_outbox = composite_pipeline_config[2]
    # Get template
    tasks = []
    # Task 1 is the NDM_24100051 pipeline with items
    tasks.append(
        argo_dag_task(
            template_ref_name=environ().ARGO_TEMPLATE_NAME,
            task_name=ndm_24100051.name,
            script_name="{{item.script_name}}",
            args="{{item.args}}",
            with_items=with_items[DataSource.ndm_24100051.value],
            depends=ndm_24100051.depends,
        )
    )
    # Task 2 is the NDM_24100054 pipeline with items
    tasks.append(
        argo_dag_task(
            template_ref_name=environ().ARGO_TEMPLATE_NAME,
            task_name=ndm_24100054.name,
            script_name="{{item.script_name}}",
            args="{{item.args}}",
            with_items=with_items[DataSource.ndm_24100054.value],
            depends=ndm_24100054.depends,
        )
    )
    # Task 3 is to run the ndm prepare outbox CLI program
    tasks.append(
        argo_dag_task(
            template_ref_name=environ().ARGO_TEMPLATE_NAME,
            task_name=ndm_outbox.name,
            script_name=ndm_outbox.cli_script,
            args=f"--date {date} --periods {num_periods} ",
            depends=ndm_outbox.depends,
        )
    )
    name = environ().ARGO_TEMPLATE_ENTRYPOINT
    dag = DagTemplate(tasks=tasks)
    metadata = Metadata(labels={"pipelineName": f"NDM Outbox ({date})"})
    composite_template = Template(name=name)
    composite_template.metadata = metadata
    composite_template.dag = dag
    return composite_template


def argo_composite_template(composite_pipeline: str, cmd: List[str]) -> DagTemplate:
    # Get composite pipeline definition
    composite_pipeline_obj = fc_composite_pipelines()[composite_pipeline]
    workflow_template_name = environ().ARGO_TEMPLATE_NAME
    tasks = []
    for obj in composite_pipeline_obj:
        tasks.append(
            argo_dag_task(
                workflow_template_name,
                obj.name,
                obj.depends,
                obj.cli_script,
                obj.get_cli_args(cmd),
            )
        )
    name = environ().ARGO_TEMPLATE_ENTRYPOINT
    dag = DagTemplate(tasks=tasks)
    metadata = Metadata(labels={"pipelineName": composite_pipeline})
    composite_template = Template(name=name)
    composite_template.metadata = metadata
    composite_template.dag = dag
    return composite_template


def argo_single_pipeline(cli_script: str, cmd: List[str], pipeline_name: str):
    api_version = ArgoConfig().api_version
    kind = "Workflow"
    metadata = argo_metadata()
    # Get template
    template = argo_single_template(cli_script, cmd, pipeline_name)
    spec = argo_spec(template)
    pipeline_workflow = Workflow(metadata=metadata, spec=spec)
    pipeline_workflow.api_version = api_version
    pipeline_workflow.kind = kind
    return pipeline_workflow


def argo_composite_pipeline(composite_pipeline: str, cmd: List[str]):
    api_version = ArgoConfig().api_version
    kind = "Workflow"
    metadata = argo_metadata()
    # Get template
    template = argo_composite_template(composite_pipeline, cmd)
    spec = argo_spec(template)
    pipeline_workflow = Workflow(metadata=metadata, spec=spec)
    pipeline_workflow.api_version = api_version
    pipeline_workflow.kind = kind
    return pipeline_workflow


def argo_ndm_output_pipeline(num_periods: str, date: str):
    api_version = ArgoConfig().api_version
    kind = "Workflow"
    metadata = argo_metadata()
    # Construct with_items list based on the number of periods given
    with_items: Dict[str, List] = {
        DataSource.ndm_24100051.value: [],
        DataSource.ndm_24100054.value: [],
    }
    for ndm_table in [DataSource.ndm_24100051.value, DataSource.ndm_24100054.value]:
        for obj in pipeline_template_instances(
            "run_pipeline.py",
            lambda d: f"--pipeline-class PipelineAggregated --data-source {ndm_table} --date {d}",
            num_periods,
            date,
        ):
            with_items[ndm_table].append(obj)

    template = argo_ndm_with_items_template(num_periods, date, with_items)
    spec = argo_spec(template)
    pipeline_workflow = Workflow(metadata=metadata, spec=spec)
    pipeline_workflow.api_version = api_version
    pipeline_workflow.kind = kind
    return pipeline_workflow


def argo_list_workflows(n: int = 30) -> List:
    """
    A function that lists `n` most recent argo workflows.

    Args:
        n: int
            The number of most recent workflows we want to return.

    Note to Maintainers:
    The implementation of this method assumes that each workflow
    runs exactly one template. At the time of writing, there is no need
    for workflows to require more than one template. If this changes at
    any point in the future, this function and argo_entrypoint
    may have to be refactored.
    """
    # Get argo host and namespace from environment variable
    # ARGO_HOST = "http://localhost:2746"
    ARGO_HOST = environ().ARGO_HOST
    ARGO_NAMESPACE = environ().ARGO_NAMESPACE
    # Need the service account JWT in order to submit requests on behalf
    # of the current webapp service account.
    WEBAPP_SA_JWT = get_fresh_token(environ().WEBAPP_SERVICE_ACCOUNT_JWT)
    warnings.warn(
        """
        Note for maintainers:

        In the future, instead of using the requests library 
        it might be helpful to use the WorkflowServiceApi.list_workflows 
        method to get the list of workflows.
        """
    )
    url = f"{ARGO_HOST}/api/v1/workflows/{ARGO_NAMESPACE}"
    payload = {}
    headers = {"Authorization": f"Bearer {WEBAPP_SA_JWT.get_secret_value()}"}
    response = requests.request("GET", url, headers=headers, data=payload)
    if response.status_code == 200:
        data = response.json()
        if data["items"]:
            data_obj = [Workflow(**obj) for obj in data["items"]]
            if "-alpha-" in environ().ARGO_TEMPLATE_NAME:
                data_obj = [
                    obj for obj in data_obj if "-alpha-" in obj.metadata.name
                ]
            else:
                data_obj = [
                    obj for obj in data_obj if "-alpha-" not in obj.metadata.name
                ]
            return list(
                itertools.islice(
                    [
                        {
                            "name": obj.spec.templates[0].metadata.labels[
                                "pipelineName"
                            ]
                            if obj.spec.templates
                            else "Direct Run from Argo UI",
                            "status": get_pipeline_status(obj.status),
                            "created_at": obj.status.started_at.root.strftime(
                                "%Y-%m-%dT%H:%M:%SZ"
                            ),
                            "finished_at": obj.status.finished_at.root.strftime(
                                "%Y-%m-%dT%H:%M:%SZ"
                            )
                            if obj.status.finished_at
                            else None,
                            "workflow_id": obj.metadata.name,
                        }
                        for obj in data_obj
                        # Only include workflows that are from the frontier-counts Workflow Template
                        if "frontier-counts" in obj.metadata.name
                    ],
                    n,
                )
            )


def get_pipeline_status(status: WorkflowStatus) -> FrontierCountsStatusCodes:
    """
    Helper to get application-specific status from process exit code.
    """
    # By default, set the STATUS_CODE to status.phase
    STATUS_CODE = status.phase
    # See if a custom error code was raised, in which case override the STATUS_CODE
    # Note: this code assumes that exactly one error code is raised, at which point
    # the entire pipeline failed. If there are exceptions to this (e.g. pipelines
    # that continue after failure), then the code below no longer guarantees a

    # If there are too many nodes in the DAG, argo will return a base64 encoded
    # set of nodes. Getting status codes for such large work flows is not part of
    # the Frontier Counts system functionality, so if such a case is encountered
    # simply use the status.phase attribute.
    if status.nodes:
        for _, node in status.nodes.items():
            if node.outputs:
                if node.outputs.exit_code not in [
                    ArgoStatusCodes.SUCCESS.value,
                    ArgoStatusCodes.RUNTIME_FAILURE.value,
                ]:
                    STATUS_CODE = (
                        STATUS_CODE_MAP[node.outputs.exit_code]
                        if node.outputs.exit_code in STATUS_CODE_MAP.keys()
                        else status.phase
                    )
    return STATUS_CODE


def argo_entrypoint(cli_script: str, cmd: List[str], pipeline_name: str):
    """
    Args:
        cli_script: str
            The name of the CLI script that is to be run.
            Example values: 'loader.py', 'run_pipeline.py'
        cmd: List
            A list of commands that will be run inside of an argo container.
            Example value: ["--pipeline-class", "PipelineRawClean", "--data-source", "pik", ...]
        pipeline_name: str
            A human readable description to name the pipeline.
    """
    # Get argo host and namespace from environment variable
    ARGO_HOST = environ().ARGO_HOST
    # ARGO_HOST = "http://localhost:2746"
    ARGO_NAMESPACE = environ().ARGO_NAMESPACE
    # Need the service account JWT in order to submit requests on behalf
    # of the current webapp service account.
    WEBAPP_SA_JWT = get_fresh_token(environ().WEBAPP_SERVICE_ACCOUNT_JWT)
    # Create configuration/client instances for the argo client
    config = Configuration(host=ARGO_HOST)
    # Note that we need to pass the auth header with JWT token to the ApiClient in
    # order for requests to get authenticated correctly.
    client = ApiClient(
        configuration=config,
        header_name="Authorization",
        header_value=f"Bearer {WEBAPP_SA_JWT.get_secret_value()}",
    )
    # Create instance of workflow service in argo python CLI
    workflow_service = WorkflowServiceApi(api_client=client)
    # Get pydantic model of Argo workflow
    argo_model = argo_single_pipeline(cli_script, cmd, pipeline_name)
    # Submit workflow to service
    workflow_service.create_workflow(
        ARGO_NAMESPACE,
        IoArgoprojWorkflowV1alpha1WorkflowCreateRequest(
            workflow=argo_model.model_dump(by_alias=True),
            # argo-workflows SDK >= 6 type-checks this field and rejects a plain
            # dict; the payload is already shaped by our pydantic Argo models.
            _check_type=False,
        ),
        # The server's reply describes a workflow that has not started yet, so
        # status.startedAt is null; the SDK types it as a non-nullable datetime
        # and raises ApiTypeError even though the workflow was created.
        _check_return_type=False,
    )


def argo_dag_entrypoint(composite_pipeline: str, cmd: List[str]):
    # Get argo host and namespace from environment variable
    ARGO_HOST = environ().ARGO_HOST
    # ARGO_HOST = "http://localhost:2746"
    ARGO_NAMESPACE = environ().ARGO_NAMESPACE
    # Need the service account JWT in order to submit requests on behalf
    # of the current webapp service account.
    WEBAPP_SA_JWT = get_fresh_token(environ().WEBAPP_SERVICE_ACCOUNT_JWT)
    # Create configuration/client instances for the argo client
    config = Configuration(host=ARGO_HOST)
    # Note that we need to pass the auth header with JWT token to the ApiClient in
    # order for requests to get authenticated correctly.
    client = ApiClient(
        configuration=config,
        header_name="Authorization",
        header_value=f"Bearer {WEBAPP_SA_JWT.get_secret_value()}",
    )
    # Create instance of workflow service in argo python CLI
    workflow_service = WorkflowServiceApi(api_client=client)
    # Get pydantic model for the workflow
    argo_model = argo_composite_pipeline(composite_pipeline, cmd)
    # Update the pipeline name to include the date. Note that since `date`` is a
    # required argument, cmd will always have the following structure:
    # cmd = ['--date', 'YYYY-MM-DD', '--reference_year', 'YYYY']
    # where the `reference_year` is optional.
    pipeline_date = cmd[1]
    current_name = argo_model.spec.templates[0].metadata.labels["pipelineName"]
    argo_model.spec.templates[0].metadata.labels[
        "pipelineName"
    ] = f"{current_name} ({pipeline_date})"
    # Submit workflow to service
    workflow_service.create_workflow(
        ARGO_NAMESPACE,
        IoArgoprojWorkflowV1alpha1WorkflowCreateRequest(
            workflow=argo_model.model_dump(by_alias=True),
            # argo-workflows SDK >= 6 type-checks this field and rejects a plain
            # dict; the payload is already shaped by our pydantic Argo models.
            _check_type=False,
        ),
        # The server's reply describes a workflow that has not started yet, so
        # status.startedAt is null; the SDK types it as a non-nullable datetime
        # and raises ApiTypeError even though the workflow was created.
        _check_return_type=False,
    )


def argo_ndm_out_entrypoint(num_periods: int, date: str):
    """
    Prepares the Argo task dag that re-runs the NDM pipelines prior to
    running the prepare-outbox script.
    """
    # Get argo host and namespace from environment variable
    ARGO_HOST = environ().ARGO_HOST
    # ARGO_HOST = "http://localhost:2746"
    ARGO_NAMESPACE = environ().ARGO_NAMESPACE
    # Need the service account JWT in order to submit requests on behalf
    # of the current webapp service account.
    WEBAPP_SA_JWT = get_fresh_token(environ().WEBAPP_SERVICE_ACCOUNT_JWT)
    # Create configuration/client instances for the argo client
    config = Configuration(host=ARGO_HOST)
    # Note that we need to pass the auth header with JWT token to the ApiClient in
    # order for requests to get authenticated correctly.
    client = ApiClient(
        configuration=config,
        header_name="Authorization",
        header_value=f"Bearer {WEBAPP_SA_JWT.get_secret_value()}",
    )
    # Create instance of workflow service in argo python CLI
    workflow_service = WorkflowServiceApi(api_client=client)
    # Get pydantic model for the workflow
    argo_model = argo_ndm_output_pipeline(num_periods, date)
    # Submit workflow to service
    workflow_service.create_workflow(
        ARGO_NAMESPACE,
        IoArgoprojWorkflowV1alpha1WorkflowCreateRequest(
            workflow=argo_model.model_dump(by_alias=True),
            # argo-workflows SDK >= 6 type-checks this field and rejects a plain
            # dict; the payload is already shaped by our pydantic Argo models.
            _check_type=False,
        ),
        # The server's reply describes a workflow that has not started yet, so
        # status.startedAt is null; the SDK types it as a non-nullable datetime
        # and raises ApiTypeError even though the workflow was created.
        _check_return_type=False,
    )
