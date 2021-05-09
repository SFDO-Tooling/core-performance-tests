import typing as T
from pathlib import Path
import logging
from contextlib import contextmanager
import shutil
import json
from traceback import format_exc
from cumulusci.core.exceptions import ServiceNotConfigured
from cumulusci.core.config import TaskConfig
from cumulusci.core.utils import import_global

from cumulusci.core.config import (
    UniversalConfig,
    ServiceConfig,
    BaseProjectConfig,
    OrgConfig,
)


def get_annotations(cls: type):
    "https://stackoverflow.com/questions/64309238/is-there-built-in-method-to-get-all-annotations-from-all-base-classes-in-pyt"
    all_ann = [c.__annotations__ for c in cls.mro()[:-1]]
    all_ann_names = set()
    for aa in all_ann[::-1]:
        all_ann_names.update(aa.keys())
    return all_ann_names


class SharedConfig:
    task_class: type
    project_config: BaseProjectConfig
    org_config: OrgConfig
    failures_dir: Path
    redirect_logging: bool
    connected_app: ServiceConfig  # can this be computed

    def __init__(self, validate: bool = False, **kwargs):
        valid_property_names = get_annotations(self.__class__)
        for k, v in kwargs.items():
            if validate and k not in valid_property_names:
                raise AssertionError(
                    f"Unknown property `{k}`. Should be one of {valid_property_names}"
                )
            setattr(self, k, v)
        for k in self.__class__.__annotations__:
            assert hasattr(self, k), f"Did not specify {k}"


# TODO: investigate dataclass-json
class WorkerConfig(SharedConfig):
    connected_app: ServiceConfig
    working_dir: Path
    task_options: T.Mapping

    def as_dict(self):
        return {
            "task_class": dotted_class_name(self.task_class),
            "org_config_class": dotted_class_name(self.org_config.__class__),
            "task_options": self.task_options,
            "working_dir": str(self.working_dir),
            "output_dir": str(self.output_dir),
            "failures_dir": str(self.failures_dir),
            "org_config": (
                self.org_config.config,
                self.org_config.name,
            ),
            "connected_app": self.connected_app.config if self.connected_app else None,
            "redirect_logging": self.redirect_logging,
            "project_config": {
                "project": {"package": self.project_config.config["project"]["package"]}
            },
        }

    @staticmethod
    def from_dict(worker_config_json):
        org_config_class = import_global(worker_config_json["org_config_class"])
        org_config = org_config_class(*worker_config_json["org_config"])

        task_options = worker_config_json["task_options"]

        universal_config = UniversalConfig()
        project_config = BaseProjectConfig(
            universal_config,
            config=worker_config_json["project_config"],
        )
        return WorkerConfig(
            task_class=import_global(worker_config_json["task_class"]),
            task_options=task_options,
            project_config=project_config,
            org_config=org_config,
            working_dir=Path(worker_config_json["working_dir"]),
            output_dir=Path(worker_config_json["output_dir"]),
            failures_dir=Path(worker_config_json["failures_dir"]),
            connected_app=ServiceConfig(worker_config_json["connected_app"])
            if worker_config_json["connected_app"]
            else None,
            redirect_logging=worker_config_json["redirect_logging"],
        )


def dotted_class_name(cls):
    return cls.__module__ + "." + cls.__name__


class TaskWorker:
    def __init__(self, worker_dict):
        self.worker_config = WorkerConfig.from_dict(worker_dict)
        self.redirect_logging = worker_dict["redirect_logging"]

    def __getattr__(self, name):
        return getattr(self.worker_config, name)

    def _make_task(self, task_class, logger):
        if "working_directory" in self.task_class.task_options:
            self.task_options["working_directory"] = self.worker_config.working_dir
        task_config = TaskConfig({"options": self.task_options})
        connected_app = self.connected_app
        keychain = SubprocessKeyChain(connected_app)
        self.project_config.set_keychain(keychain)
        self.org_config.keychain = keychain
        return task_class(
            project_config=self.project_config,
            task_config=task_config,
            org_config=self.org_config,
            logger=logger,
        )

    def save_exception(self, e):
        exception_file = self.working_dir / "exception.txt"
        exception_file.write_text(format_exc())

    def run(self):
        with self.make_logger() as logger:
            try:
                self.subtask = self._make_task(self.task_class, logger)
                self.subtask()
            except BaseException as e:
                logger.info(f"Failure detected: {e}")
                self.save_exception(e)
                self.failures_dir.mkdir(exist_ok=True)
                shutil.move(str(self.working_dir), str(self.failures_dir))
                raise

            try:
                self.output_dir.mkdir(exist_ok=True)
                shutil.move(str(self.working_dir), str(self.output_dir))
                logger.info("SubTask Success!")
            except BaseException as e:
                logger.info(f"Failure detected: {e}")
                self.save_exception(e)
                raise

    @contextmanager
    def make_logger(self):
        filename = self.working_dir / f"{self.task_class.__name__}.log"
        with filename.open("w") as f:
            logger = logging.Logger(self.task_class.__name__)

            formatter = logging.Formatter(fmt="%(asctime)s: %(message)s")
            handler = logging.StreamHandler(stream=f)
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            logger.propagate = False
            yield logger


def run_task_in_worker(worker_dict):
    worker = TaskWorker(worker_dict)
    return worker.run()


def simplify(x):
    if isinstance(x, Path):
        return str(x)
    if hasattr(x, "isoformat"):
        return x.isoformat()


class ParallelWorker:
    def __init__(self, spawn_class, worker_config: WorkerConfig):
        self.spawn_class = spawn_class
        self.worker_config = worker_config

    def _validate_worker_config_is_simple(self, worker_config):
        assert json.dumps(worker_config, default=simplify)

    def start(self):
        dct = self.worker_config.as_dict()
        self._validate_worker_config_is_simple(dct)

        self.process = self.spawn_class(
            target=run_task_in_worker, args=[dct], daemon=True
        )
        self.process.start()

    def is_alive(self):
        return self.process.is_alive()

    def join(self):
        return self.process.join()

    def terminate(self):
        # Note that this will throw an exception for threads
        # and should be used carefully for processes because
        # they won't necesssarily cleanup tempdiirs and other
        # resources.
        self.process.terminate()

    def __repr__(self):
        return f"<Worker {self.worker_config.task_class.__name__} {self.worker_config.working_dir.name} Alive: {self.is_alive()}>"


class SubprocessKeyChain(T.NamedTuple):
    connected_app: T.Any = None

    def get_service(self, name):
        if name == "connected_app" and self.connected_app:
            return self.connected_app

        raise ServiceNotConfigured(name)

    def set_org(self, *args):
        pass
