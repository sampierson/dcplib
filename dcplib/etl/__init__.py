"""
Shared ETL (extract, transform, load) code for fetching and loading data from HCA DSS.

The main entry point is the DSSExtractor.etl_bundles() function. The DSSExtractor takes optional transform, load, and
finalize callbacks that will be invoked for each bundle fetched. The etl_bundles function takes

Example usage:

    from dcplib.etl import DSSExtractor

    def tf(*args, **kwargs):
        print("Transformer", args, kwargs)
        return "TEST"

    def ld(*args, **kwargs):
        print("Loader", args, kwargs)

    def fn(*args, **kwargs):
        print("Finalizer", args, kwargs)

    DSSExtractor(staging_directory=".", transformer=tf, loader=ld, finalizer=fn).etl_bundles()
"""

import os, sys, json, concurrent.futures, hashlib, logging, threading
from collections import defaultdict
from fnmatch import fnmatchcase

import hca
from ..networking import http
from hca.util import RetryPolicy

logger = logging.getLogger(__name__)


class DSSExtractor:
    default_bundle_query = {'query': {'bool': {'must_not': {'term': {'admin_deleted': True}}}}}
    default_content_type_patterns = ['application/json; dcp-type="metadata*"']

    def __init__(self, staging_directory, content_type_patterns: list = None, filename_patterns: list = None,
                 dss_client: hca.dss.DSSClient = None, dispatch_on_empty_bundles=False,
                 transformer: callable = None, loader: callable = None, finalizer: callable = None):
        self.sd = staging_directory
        self.content_type_patterns = content_type_patterns or self.default_content_type_patterns
        self.filename_patterns = filename_patterns or []
        self._dss_client = dss_client
        self._dss_swagger_url = None
        # what is this for?
        self._dispatch_on_empty_bundles = dispatch_on_empty_bundles
        self.transformer = transformer
        self.loader = loader
        self.finalizer = finalizer

        # concurrent.futures.ProcessPoolExecutor requires objects to be picklable.
        # hca.dss.DSSClient is unpicklable and is stubbed out here to preserve DSSExtractor's picklability.

    def __getstate__(self):
        state = dict(self.__dict__)
        state["_dss_swagger_url"] = self.dss_client.swagger_url
        state["_dss_client"] = None
        return state

    @property
    def dss_client(self):
        if self._dss_client is None:
            self._dss_client = hca.dss.DSSClient(swagger_url=self._dss_swagger_url)
        return self._dss_client

    def etl_one_bundle(self, bundle_uuid, bundle_version):
        # get manifest and files
        bundle_uuid, bundle_version, fetched_files = self.get_files_to_fetch_for_bundle(bundle_uuid, bundle_version)
        bundle_path = f"{self.sd}/bundles/{bundle_uuid}.{bundle_version}"
        bundle_manifest_path = f"{self.sd}/bundle_manifests/{bundle_uuid}.{bundle_version}.json"
        if not os.path.exists(bundle_path):
            if self._dispatch_on_empty_bundles:
                bundle_path = None
            else:
                return
        if self.transformer is not None:
            tb = self.transformer(bundle_uuid=bundle_uuid, bundle_version=bundle_version, bundle_path=bundle_path,
                                  bundle_manifest_path=bundle_manifest_path, extractor=self)

        return tb or None

    def etl_bundles(self, query, max_workers=512, max_dispatchers=1,
                    dispatch_executor_class: concurrent.futures.Executor = concurrent.futures.ThreadPoolExecutor):
        futures = []
        if query is None:
            query = self.default_bundle_query
        total_bundles = self.dss_client.post_search(es_query=query, replica="aws")["total_hits"]
        if total_bundles == 0:
            logger.error("No bundles found, nothing to do")
            return
        logger.info("Scanning %s bundles", total_bundles)
        bundles = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            for b in self.dss_client.post_search.iterate(es_query=query, replica="aws", per_page=500):
                bundle_uuid, bundle_version = b["bundle_fqid"].split(".", 1)
                futures.append(executor.submit(self.etl_one_bundle, bundle_uuid, bundle_version))

        for future in concurrent.futures.as_completed(futures):
            bundle = future.result()
            bundles.append(bundle)

        if self.loader is not None:
            self.loader(bundles=bundles)
        # call finalizer
        if self.finalizer is not None:
            self.finalizer(extractor=self)

    def get_files_to_fetch_for_bundle(self, bundle_uuid, bundle_version):
        # get the bundle manifest and store in file system
        try:
            with open(f"{self.sd}/bundle_manifests/{bundle_uuid}.{bundle_version}.json") as fh:
                bundle_manifest = json.load(fh)
            logger.debug("[%s] Loaded cached manifest for bundle %s", threading.current_thread().getName(), bundle_uuid)
        except (FileNotFoundError, json.decoder.JSONDecodeError):
            logger.debug("[%s] Fetching manifest for bundle %s", threading.current_thread().getName(), bundle_uuid)
            res = http.get(f"{self.dss_client.host}/bundles/{bundle_uuid}", params={"replica": "aws"})
            res.raise_for_status()
            bundle_manifest = res.json()["bundle"]
            os.makedirs(f"{self.sd}/bundle_manifests", exist_ok=True)
            with open(f"{self.sd}/bundle_manifests/{bundle_uuid}.{bundle_version}.json", "w") as fh:
                json.dump(bundle_manifest, fh)

        logger.debug("Scanning bundle %s", bundle_uuid)
        fetched_files = []
        # for each file in the bundle manifest get file and store in file system
        for f in bundle_manifest["files"]:
            if self._should_fetch_file(f):
                os.makedirs(f"{self.sd}/bundles/{bundle_uuid}.{bundle_version}", exist_ok=True)
                os.makedirs(f"{self.sd}/files/{bundle_uuid}.{bundle_version}", exist_ok=True)
                try:
                    with open(f"{self.sd}/files/{f['uuid']}.{f['version']}", "rb") as fh:
                        file_csum = hashlib.sha256(fh.read()).hexdigest()
                        if file_csum == f["sha256"]:
                            self._link_file(bundle_uuid, bundle_version, f)
                            continue
                except (FileNotFoundError,):
                    self._get_file(f, bundle_uuid, bundle_version)
                    fetched_files.append(f)
            else:
                logger.debug("Skipping file %s/%s (no filter match)", bundle_uuid, f["name"])
        return bundle_uuid, bundle_version, fetched_files

    def _should_fetch_file(self, f):
        if any(fnmatchcase(f["content-type"], p) for p in self.content_type_patterns):
            return True
        if any(fnmatchcase(f["name"], p) for p in self.filename_patterns):
            return True
        return False

    def _link_file(self, bundle_uuid, bundle_version, f):
        if not os.path.exists(f"{self.sd}/bundles/{bundle_uuid}.{bundle_version}/{f['name']}"):
            logger.debug("Linking fetched file %s/%s", bundle_uuid, f["name"])
            os.symlink(f"../../files/{f['uuid']}.{f['version']}",
                       f"{self.sd}/bundles/{bundle_uuid}.{bundle_version}/{f['name']}")

    def _get_file(self, f, bundle_uuid, bundle_version, print_progress=True):
        logger.debug("[%s] Fetching %s:%s", threading.current_thread().getName(), bundle_uuid, f["name"])
        res = http.get(f"{self.dss_client.host}/files/{f['uuid']}", params={"replica": "aws", "version": f["version"]})
        res.raise_for_status()
        with open(f"{self.sd}/files/{f['uuid']}.{f['version']}", "wb") as fh:
            fh.write(res.content)
        self._link_file(bundle_uuid, bundle_version, f)
        logger.debug("Wrote %s:%s", bundle_uuid, f["name"])
        if print_progress:
            sys.stdout.write(".")
            sys.stdout.flush()
        return f, bundle_uuid, bundle_version
