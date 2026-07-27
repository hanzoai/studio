import studio_compat  # noqa: F401  aliases comfy.* -> studio.* for upstream custom nodes
import studio.options
studio.options.enable_args_parsing()

import os
import importlib.util
import folder_paths
import time
from studio.cli_args import args, enables_dynamic_vram
from app.logger import setup_logger
from app.assets.scanner import seed_assets
import itertools
import utils.extra_config
import logging
import sys
from studio_execution.progress import get_progress_state
from studio_execution.utils import get_executing_context
from studio_api import feature_flags


if __name__ == "__main__":
    #NOTE: These do not do anything on core Hanzo Studio, they are for custom nodes.
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['DO_NOT_TRACK'] = '1'

setup_logger(log_level=args.verbose, use_stdout=args.log_stdout)

if os.name == "nt":
    os.environ['MIMALLOC_PURGE_DELAY'] = '0'

if __name__ == "__main__":
    os.environ['TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL'] = '1'
    if args.default_device is not None:
        default_dev = args.default_device
        devices = list(range(32))
        devices.remove(default_dev)
        devices.insert(0, default_dev)
        devices = ','.join(map(str, devices))
        os.environ['CUDA_VISIBLE_DEVICES'] = str(devices)
        os.environ['HIP_VISIBLE_DEVICES'] = str(devices)

    if args.cuda_device is not None:
        os.environ['CUDA_VISIBLE_DEVICES'] = str(args.cuda_device)
        os.environ['HIP_VISIBLE_DEVICES'] = str(args.cuda_device)
        os.environ["ASCEND_RT_VISIBLE_DEVICES"] = str(args.cuda_device)
        logging.info("Set cuda device to: {}".format(args.cuda_device))

    if args.oneapi_device_selector is not None:
        os.environ['ONEAPI_DEVICE_SELECTOR'] = args.oneapi_device_selector
        logging.info("Set oneapi device selector to: {}".format(args.oneapi_device_selector))

    if args.deterministic:
        if 'CUBLAS_WORKSPACE_CONFIG' not in os.environ:
            os.environ['CUBLAS_WORKSPACE_CONFIG'] = ":4096:8"

    import cuda_malloc
    if "rocm" in cuda_malloc.get_torch_version_noimport():
        os.environ['OCL_SET_SVM_SIZE'] = '262144'  # set at the request of AMD


def handle_comfyui_manager_unavailable():
    if not args.windows_standalone_build:
        logging.warning(f"\n\nYou appear to be running the studio manager from source, this is not recommended. Please install it using the following command:\ncommand:\n\t{sys.executable} -m pip install --pre comfyui_manager\n")
    args.enable_manager = False


if args.enable_manager:
    if importlib.util.find_spec("comfyui_manager"):
        import comfyui_manager

        if not comfyui_manager.__file__ or not comfyui_manager.__file__.endswith('__init__.py'):
            handle_comfyui_manager_unavailable()
    else:
        handle_comfyui_manager_unavailable()


def apply_custom_paths():
    # extra model paths
    extra_model_paths_config_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "extra_model_paths.yaml")
    if os.path.isfile(extra_model_paths_config_path):
        utils.extra_config.load_extra_path_config(extra_model_paths_config_path)

    if args.extra_model_paths_config:
        for config_path in itertools.chain(*args.extra_model_paths_config):
            utils.extra_config.load_extra_path_config(config_path)

    # --output-directory, --input-directory, --user-directory
    if args.output_directory:
        output_dir = os.path.abspath(args.output_directory)
        logging.info(f"Setting output directory to: {output_dir}")
        folder_paths.set_output_directory(output_dir)

    # These are the default folders that checkpoints, clip and vae models will be saved to when using CheckpointSave, etc.. nodes
    folder_paths.add_model_folder_path("checkpoints", os.path.join(folder_paths.get_output_directory(), "checkpoints"))
    folder_paths.add_model_folder_path("clip", os.path.join(folder_paths.get_output_directory(), "clip"))
    folder_paths.add_model_folder_path("vae", os.path.join(folder_paths.get_output_directory(), "vae"))
    folder_paths.add_model_folder_path("diffusion_models",
                                       os.path.join(folder_paths.get_output_directory(), "diffusion_models"))
    folder_paths.add_model_folder_path("loras", os.path.join(folder_paths.get_output_directory(), "loras"))

    if args.input_directory:
        input_dir = os.path.abspath(args.input_directory)
        logging.info(f"Setting input directory to: {input_dir}")
        folder_paths.set_input_directory(input_dir)

    if args.user_directory:
        user_dir = os.path.abspath(args.user_directory)
        logging.info(f"Setting user directory to: {user_dir}")
        folder_paths.set_user_directory(user_dir)


def execute_prestartup_script():
    if args.disable_all_custom_nodes and len(args.whitelist_custom_nodes) == 0:
        return

    def execute_script(script_path):
        module_name = os.path.splitext(script_path)[0]
        try:
            spec = importlib.util.spec_from_file_location(module_name, script_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return True
        except Exception as e:
            logging.error(f"Failed to execute startup-script: {script_path} / {e}")
        return False

    node_paths = folder_paths.get_folder_paths("custom_nodes")
    for custom_node_path in node_paths:
        possible_modules = os.listdir(custom_node_path)
        node_prestartup_times = []

        for possible_module in possible_modules:
            module_path = os.path.join(custom_node_path, possible_module)

            if args.enable_manager:
                if comfyui_manager.should_be_disabled(module_path):
                    continue

            if os.path.isfile(module_path) or module_path.endswith(".disabled") or module_path == "__pycache__":
                continue

            script_path = os.path.join(module_path, "prestartup_script.py")
            if os.path.exists(script_path):
                if args.disable_all_custom_nodes and possible_module not in args.whitelist_custom_nodes:
                    logging.info(f"Prestartup Skipping {possible_module} due to disable_all_custom_nodes and whitelist_custom_nodes")
                    continue
                time_before = time.perf_counter()
                success = execute_script(script_path)
                node_prestartup_times.append((time.perf_counter() - time_before, module_path, success))
    if len(node_prestartup_times) > 0:
        logging.info("\nPrestartup times for custom nodes:")
        for n in sorted(node_prestartup_times):
            if n[2]:
                import_message = ""
            else:
                import_message = " (PRESTARTUP FAILED)"
            logging.info("{:6.1f} seconds{}: {}".format(n[0], import_message, n[1]))
        logging.info("")

apply_custom_paths()

if args.enable_manager:
    comfyui_manager.prestartup()

execute_prestartup_script()


# Main code
import asyncio
import shutil
import threading
import gc

if 'torch' in sys.modules:
    logging.warning("WARNING: Potential Error in code: Torch already imported, torch should never be imported before this point.")

import comfy_aimdo.control

if enables_dynamic_vram():
    comfy_aimdo.control.init()

import studio.utils

import execution
import server
from protocol import BinaryEventTypes
import nodes
import studio.model_management
import studio_version
import app.logger
import hook_breaker_ac10a0

import studio.memory_management
import studio.model_patcher

if enables_dynamic_vram():
    if studio.model_management.torch_version_numeric < (2, 8):
        logging.warning("Unsupported Pytorch detected. DynamicVRAM support requires Pytorch version 2.8 or later. Falling back to legacy ModelPatcher. VRAM estimates may be unreliable especially on Windows")
    elif comfy_aimdo.control.init_device(studio.model_management.get_torch_device().index):
        if args.verbose == 'DEBUG':
            comfy_aimdo.control.set_log_debug()
        elif args.verbose == 'CRITICAL':
            comfy_aimdo.control.set_log_critical()
        elif args.verbose == 'ERROR':
            comfy_aimdo.control.set_log_error()
        elif args.verbose == 'WARNING':
            comfy_aimdo.control.set_log_warning()
        else: #INFO
            comfy_aimdo.control.set_log_info()

        studio.model_patcher.CoreModelPatcher = studio.model_patcher.ModelPatcherDynamic
        studio.memory_management.aimdo_enabled = True
        logging.info("DynamicVRAM support detected and enabled")
    else:
        logging.warning("No working comfy-aimdo install detected. DynamicVRAM support disabled. Falling back to legacy ModelPatcher. VRAM estimates may be unreliable especially on Windows")


def cuda_malloc_warning():
    device = studio.model_management.get_torch_device()
    device_name = studio.model_management.get_torch_device_name(device)
    cuda_malloc_warning = False
    if "cudaMallocAsync" in device_name:
        for b in cuda_malloc.blacklist:
            if b in device_name:
                cuda_malloc_warning = True
        if cuda_malloc_warning:
            logging.warning("\nWARNING: this card most likely does not support cuda-malloc, if you get \"CUDA error\" please run Hanzo Studio with: --disable-cuda-malloc\n")


def prompt_worker(q, server_instance):
    current_time: float = 0.0
    cache_type = execution.CacheType.CLASSIC
    if args.cache_lru > 0:
        cache_type = execution.CacheType.LRU
    elif args.cache_ram > 0:
        cache_type = execution.CacheType.RAM_PRESSURE
    elif args.cache_none:
        cache_type = execution.CacheType.NONE

    e = execution.PromptExecutor(server_instance, cache_type=cache_type, cache_args={ "lru" : args.cache_lru, "ram" : args.cache_ram } )
    last_gc_collect = 0
    need_gc = False
    gc_collect_interval = 10.0

    # Import metrics + billing for prompt tracking
    from middleware import metrics_middleware
    from middleware import billing_middleware
    from middleware import content_publish
    enable_billing = getattr(args, "enable_billing", False)
    enable_metrics = getattr(args, "enable_metrics", False)

    while True:
        timeout = 1000.0
        if need_gc:
            timeout = max(gc_collect_interval - (current_time - last_gc_collect), 0.0)

        queue_item = q.get(timeout=timeout)
        if queue_item is not None:
            item, item_id = queue_item
            execution_start_time = time.perf_counter()
            prompt_id = item[1]
            server_instance.last_prompt_id = prompt_id

            if enable_metrics:
                metrics_middleware.record_prompt_start()

            sensitive = item[5]
            extra_data = item[3].copy()
            for k in sensitive:
                extra_data[k] = sensitive[k]

            # Tenancy: scope this execution's output/temp/input dirs to the org.
            folder_paths.set_execution_org(extra_data.get("org_id"))
            try:
                e.execute(item[2], prompt_id, extra_data, item[4])
            finally:
                folder_paths.set_execution_org(None)
            need_gc = True

            remove_sensitive = lambda prompt: prompt[:5] + prompt[6:]
            q.task_done(item_id,
                        e.history_result,
                        status=execution.PromptQueue.ExecutionStatus(
                            status_str='success' if e.success else 'error',
                            completed=e.success,
                            messages=e.status_messages), process_item=remove_sensitive)
            if server_instance.client_id is not None:
                server_instance.send_sync("executing", {"node": None, "prompt_id": prompt_id}, server_instance.client_id)

            current_time = time.perf_counter()
            execution_time = current_time - execution_start_time

            # Record metrics
            if enable_metrics:
                metrics_middleware.record_prompt_end(execution_time, e.success)

            # Billing: emit ONE metered usage event per completed render
            # (fire-and-forget). org_id is the IAM tenant; prompt_id is the
            # idempotency key; quantity is the number of artifacts produced.
            # Never blocks or fails a render; no-op unless STUDIO_BILLING_* is set.
            if enable_billing and e.success:
                asyncio.run_coroutine_threadsafe(
                    billing_middleware.record_render(
                        org_id=extra_data.get("org_id") or "default",
                        prompt_id=prompt_id,
                        n_outputs=billing_middleware.count_outputs(e.history_result),
                    ),
                    server_instance.loop,
                )

            # Content: record each output artifact as a draft Asset in the content
            # lane (clients/content, module "marketing") — the SAME DocType that
            # lane writes when it drives a render, so a studio-initiated render lands
            # in the same place. Written as the requesting user via their IAM token
            # from `sensitive`, so it lands in their org. Assets land draft; a human
            # moves them through the lifecycle, and published is what a storefront
            # shows. Fire-and-forget: the publisher logs every refusal itself (its result
            # is for the Library's on-demand Publish to CMS), and a render is NEVER failed
            # if the lane is down.
            if content_publish.enabled() and e.success:
                asyncio.run_coroutine_threadsafe(
                    content_publish.publish_render(
                        org_id=extra_data.get("org_id") or "default",
                        prompt_id=prompt_id,
                        workflow=item[2],
                        history_result=e.history_result,
                        iam_token=extra_data.get("iam_token"),
                        extra_data=extra_data,
                    ),
                    server_instance.loop,
                )

            # Log Time in a more readable way after 10 minutes
            if execution_time > 600:
                execution_time = time.strftime("%H:%M:%S", time.gmtime(execution_time))
                logging.info(f"Prompt executed in {execution_time}")
            else:
                logging.info("Prompt executed in {:.2f} seconds".format(execution_time))

        flags = q.get_flags()
        free_memory = flags.get("free_memory", False)

        if flags.get("unload_models", free_memory):
            studio.model_management.unload_all_models()
            need_gc = True
            last_gc_collect = 0

        if free_memory:
            e.reset()
            need_gc = True
            last_gc_collect = 0

        if need_gc:
            current_time = time.perf_counter()
            if (current_time - last_gc_collect) > gc_collect_interval:
                gc.collect()
                studio.model_management.soft_empty_cache()
                last_gc_collect = current_time
                need_gc = False
                hook_breaker_ac10a0.restore_functions()


async def run(server_instance, address='', port=8188, verbose=True, call_on_start=None):
    addresses = []
    for addr in address.split(","):
        addresses.append((addr, port))
    await asyncio.gather(
        server_instance.start_multi_address(addresses, call_on_start, verbose), server_instance.publish_loop()
    )

def hijack_progress(server_instance):
    def hook(value, total, preview_image, prompt_id=None, node_id=None):
        executing_context = get_executing_context()
        if prompt_id is None and executing_context is not None:
            prompt_id = executing_context.prompt_id
        if node_id is None and executing_context is not None:
            node_id = executing_context.node_id
        studio.model_management.throw_exception_if_processing_interrupted()
        if prompt_id is None:
            prompt_id = server_instance.last_prompt_id
        if node_id is None:
            node_id = server_instance.last_node_id
        progress = {"value": value, "max": total, "prompt_id": prompt_id, "node": node_id}
        get_progress_state().update_progress(node_id, value, total, preview_image)

        server_instance.send_sync("progress", progress, server_instance.client_id)
        if preview_image is not None:
            # Only send old method if client doesn't support preview metadata
            if not feature_flags.supports_feature(
                server_instance.sockets_metadata,
                server_instance.client_id,
                "supports_preview_metadata",
            ):
                server_instance.send_sync(
                    BinaryEventTypes.UNENCODED_PREVIEW_IMAGE,
                    preview_image,
                    server_instance.client_id,
                )

    studio.utils.set_progress_bar_global_hook(hook)


def cleanup_temp():
    temp_dir = folder_paths.get_temp_directory()
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)


def setup_database():
    try:
        from app.database.db import init_db, dependencies_available
        if dependencies_available():
            init_db()
            if not args.disable_assets_autoscan:
                seed_assets(["models"], enable_logging=True)
    except Exception as e:
        logging.error(f"Failed to initialize database. Please ensure you have installed the latest requirements. If the error persists, please report this as in future the database will be required: {e}")


def start_studio(asyncio_loop=None):
    """
    Starts the Hanzo Studio server using the provided asyncio event loop or creates a new one.
    Returns the event loop, server instance, and a function to start the server asynchronously.
    """
    if args.temp_directory:
        temp_dir = os.path.join(os.path.abspath(args.temp_directory), "temp")
        logging.info(f"Setting temp directory to: {temp_dir}")
        folder_paths.set_temp_directory(temp_dir)
    cleanup_temp()

    if args.windows_standalone_build:
        try:
            import new_updater
            new_updater.update_windows_updater()
        except:
            pass

    if not asyncio_loop:
        asyncio_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(asyncio_loop)
    prompt_server = server.PromptServer(asyncio_loop)

    if args.enable_manager and not args.disable_manager_ui:
        comfyui_manager.start()

    hook_breaker_ac10a0.save_functions()
    asyncio_loop.run_until_complete(nodes.init_extra_nodes(
        init_custom_nodes=(not args.disable_all_custom_nodes) or len(args.whitelist_custom_nodes) > 0,
        init_api_nodes=not args.disable_api_nodes
    ))
    hook_breaker_ac10a0.restore_functions()

    cuda_malloc_warning()
    setup_database()

    # In worker mode, register the worker execution routes BEFORE add_routes() mounts
    # the route table — otherwise POST /v1/worker/execute is appended after the SPA
    # catch-all is already mounted and never registers, so the catch-all answers it
    # (GET/HEAD only) and the coordinator seam 405s (no hidden-run hole, but no renders).
    if args.worker_mode:
        from middleware.worker_client import add_worker_routes, WORKER_TOKEN
        # FAIL CLOSED: a worker box executes jobs the coordinator hands it over a
        # secret-authenticated seam. Without STUDIO_WORKER_TOKEN that seam (and the
        # worker-mode /prompt gate) would accept un-tokened callers — silently
        # reopening the hidden-run hole. Refuse to start rather than run wide open.
        if not WORKER_TOKEN:
            sys.exit("worker-mode requires STUDIO_WORKER_TOKEN (coordinator shared secret) — refusing to start")
        add_worker_routes(prompt_server.routes, prompt_server)
        logging.info("Worker mode enabled — worker_id=%s coordinator=%s",
                      args.worker_id, args.coordinator_url)

    prompt_server.add_routes()
    hijack_progress(prompt_server)

    _worker_thread = threading.Thread(target=prompt_worker, daemon=True, args=(prompt_server.prompt_queue, prompt_server,))
    _worker_thread.start()
    # Exposed so /ready can gate on the render worker actually being alive: if this
    # daemon thread dies (an unhandled exception in the executor), the queue still
    # accepts /prompt but nothing runs — readiness must fail so the pod leaves rotation
    # and a roll never promotes a wedged pod.
    prompt_server.prompt_worker_thread = _worker_thread

    if args.quick_test_for_ci:
        exit(0)

    os.makedirs(folder_paths.get_temp_directory(), exist_ok=True)
    call_on_start = None
    if args.auto_launch:
        def startup_server(scheme, address, port):
            import webbrowser
            if os.name == 'nt' and address == '0.0.0.0':
                address = '127.0.0.1'
            if ':' in address:
                address = "[{}]".format(address)
            webbrowser.open(f"{scheme}://{address}:{port}")
        call_on_start = startup_server

    async def start_all():
        await prompt_server.setup()

        # Start worker heartbeat loop if in worker mode
        if args.worker_mode and args.coordinator_url:
            from middleware.worker_client import WorkerClient
            worker_client = WorkerClient(
                coordinator_url=args.coordinator_url,
                worker_id=args.worker_id or f"worker-{os.getpid()}",
                worker_port=args.port,
                org_id=os.environ.get("STUDIO_ORG_ID", "default"),
            )
            await worker_client.start()
            prompt_server._worker_client = worker_client

        await run(prompt_server, address=args.listen, port=args.port, verbose=not args.dont_print_server, call_on_start=call_on_start)

    # Returning these so that other code can integrate with the Hanzo Studio loop and server
    return asyncio_loop, prompt_server, start_all


if __name__ == "__main__":
    # Running directly, just start Hanzo Studio.
    import signal

    logging.info("Python version: {}".format(sys.version))
    logging.info("Hanzo Studio version: {}".format(studio_version.__version__))

    if sys.version_info.major == 3 and sys.version_info.minor < 10:
        logging.warning("WARNING: You are using a python version older than 3.10, please upgrade to a newer one. 3.12 and above is recommended.")

    event_loop, prompt_server, start_all_func = start_studio()

    # Sentinel: a non-empty list means shutdown is already in progress. Using a
    # mutable container (not a global) keeps the handler closure-clean.
    shutdown_started = []

    def _graceful_shutdown(signum, frame):
        sig_name = signal.Signals(signum).name
        if shutdown_started:
            logging.info(f"Received {sig_name} again, already shutting down...")
            return
        shutdown_started.append(True)
        logging.info(f"Received {sig_name}, shutting down gracefully...")
        # Stop accepting new prompts and snapshot any pending work to disk so a
        # restart can pick it back up (no-op unless STUDIO_PERSIST_QUEUE=1).
        if prompt_server and prompt_server.prompt_queue:
            prompt_server.prompt_queue.set_flag("disable_new", True)
            prompt_server.prompt_queue.persist_now()
        # Ask the event loop to stop. Because the server coroutine never finishes
        # on its own, run_until_complete() then raises RuntimeError('Event loop
        # stopped before Future completed.'); the except clause below treats that
        # as the expected graceful-exit path rather than a crash.
        event_loop.call_soon_threadsafe(event_loop.stop)

    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)

    try:
        x = start_all_func()
        app.logger.print_startup_warnings()
        event_loop.run_until_complete(x)
    except KeyboardInterrupt:
        logging.info("\nStopped server")
    except RuntimeError as e:
        # A signal handler stopped the loop before the (never-completing) server
        # coroutine finished; asyncio surfaces this as RuntimeError. This is our
        # graceful-exit path, not a crash. Anything else is a real error.
        if "Event loop stopped before Future completed" in str(e):
            logging.info("Server stopped by shutdown signal")
        else:
            raise

    logging.info("Cleaning up...")
    cleanup_temp()
