"""VitEmbedder - real ViT inference via google/vit-base-patch16-224.

This module is only imported when AVSA_MODEL_STUB=0.  In stub mode (all CI
runs) this file is never imported, so torch and transformers are not required.
"""

from __future__ import annotations

import io
import logging
import os
import pathlib
import tempfile
import tomllib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import onnxruntime as ort

import torch
from PIL import Image
from transformers import AutoFeatureExtractor, AutoModel

from avsa_model.device import resolve_device
from avsa_model.heads import AttributeHeads, AttributePrediction, resolve_attribute_heads_dir

_MODEL_NAME = "google/vit-base-patch16-224"
_log = logging.getLogger(__name__)


def _load_config() -> dict[str, Any]:
    """Read the full config/avsa.toml. Returns {} if not found.

    The whole document (not just [model]) is returned so the head-artifact
    resolver can read [model] attribute_heads_dir through the same
    config-driven contract the tests pin.
    """
    for parent in [pathlib.Path(__file__).resolve(), *pathlib.Path(__file__).resolve().parents]:
        candidate = parent / "config" / "avsa.toml"
        if candidate.exists():
            with open(candidate, "rb") as f:
                return tomllib.load(f)
    return {}


class VitEmbedder:
    """Loads google/vit-base-patch16-224 once and serves embed requests.

    Model and feature extractor are loaded in __init__ - call this once at
    application startup, not per-request.

    Args:
        use_fp16: Cast model to fp16 before inference. When None, reads from
            config/avsa.toml [model].use_fp16 (default False).
        use_compile: Apply torch.compile to the model. When None, reads from
            config/avsa.toml [model].use_compile (default False).
        device: Explicit device placement (cpu|mps|cuda). When None, resolves
            from AVSA_MODEL_DEVICE then config/avsa.toml [model].device
            (default cpu) via avsa_model.device.resolve_device.
    """

    def __init__(
        self,
        use_fp16: bool | None = None,
        use_bf16: bool | None = None,
        use_compile: bool | None = None,
        use_ort: bool | None = None,
        use_trt: bool | None = None,
        device: str | None = None,
        compile_mode: str | None = None,
    ) -> None:
        config = _load_config()
        cfg = config.get("model", {})
        # AVSA_MODEL_FP16=1 mirrors AVSA_MODEL_DEVICE: env beats config beats False.
        _env_fp16 = os.environ.get("AVSA_MODEL_FP16")
        self._use_fp16: bool = (
            use_fp16
            if use_fp16 is not None
            else (_env_fp16 == "1")
            if _env_fp16 is not None
            else bool(cfg.get("use_fp16", False))
        )
        self._use_bf16: bool = (
            use_bf16 if use_bf16 is not None else bool(cfg.get("use_bf16", False))
        )
        if self._use_fp16 and self._use_bf16:
            _log.warning("use_fp16 and use_bf16 both set - use_bf16 takes precedence")
            self._use_fp16 = False
        _use_ort: bool = use_ort if use_ort is not None else bool(cfg.get("use_ort", False))
        _use_trt: bool = use_trt if use_trt is not None else bool(cfg.get("use_trt", False))
        _use_compile: bool = (
            use_compile if use_compile is not None else bool(cfg.get("use_compile", False))
        )
        _compile_mode: str = (
            compile_mode
            if compile_mode is not None
            else str(cfg.get("compile_mode", "reduce-overhead"))
        )
        _attn_impl: str = str(cfg.get("attn_implementation", "sdpa"))
        # Resolve the device once at construction (env beats config beats cpu).
        # An explicit arg short-circuits resolution so the sweep can pin a tier.
        self._device: str = device if device is not None else resolve_device(config, env=os.environ)
        _log.info("VitEmbedder device: %s", self._device)

        self._extractor: AutoFeatureExtractor = AutoFeatureExtractor.from_pretrained(_MODEL_NAME)

        # Preprocess pipeline (E5 in docs/qps-local-optimisation.md). The HF
        # ViTFeatureExtractor's PIL → numpy → resize → normalise → torch path
        # is replaceable by an equivalent torchvision.transforms.v2 pipeline
        # that stays in torch land end-to-end. Equivalence vs HF measured
        # over 32 real Fashion200k images: min embedding cosine 0.999767,
        # mean 0.999951 — the worst-case image sits just below the 0.9999
        # equivalence gate (0.00013 short), so recall@5 should be the gating
        # check on adoption rather than min-cosine in isolation. Measured QPS
        # effect: +5.4% system bench c=8, +5% in-process bs=1, -8% embedder
        # stage time at bs=8. Opt back to HF preprocess via
        # AVSA_MODEL_PREPROCESS=hf for parity testing or rollback.
        import torchvision.transforms.v2 as _v2

        self._use_v2_preprocess: bool = os.environ.get("AVSA_MODEL_PREPROCESS", "v2") == "v2"
        self._tv_preprocess = _v2.Compose(
            [
                _v2.PILToTensor(),
                _v2.Resize(
                    (224, 224),
                    interpolation=_v2.InterpolationMode.BILINEAR,
                    antialias=True,
                ),
                _v2.ToDtype(torch.float32, scale=True),
                _v2.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
            ]
        )
        try:
            self._model: AutoModel = AutoModel.from_pretrained(
                _MODEL_NAME, attn_implementation=_attn_impl
            )
            _log.info("AutoModel loaded with attn_implementation=%s", _attn_impl)
        except Exception as exc:
            _log.warning(
                "attn_implementation=%s failed (%s), falling back to sdpa", _attn_impl, exc
            )
            self._model = AutoModel.from_pretrained(_MODEL_NAME, attn_implementation="sdpa")
        self._model.eval()

        # Move the model onto the selected device before any optimisation so
        # fp16/compile operate on the device-resident weights.
        self._model = self._model.to(self._device)

        if self._use_fp16:
            self._model = self._model.half()

        if self._use_bf16:
            self._model = self._model.bfloat16()

        # ORT replaces the torch inference path entirely; build the session from the
        # already-dtype-cast model before compile (compiled models are not ONNX-exportable).
        self._ort_session: ort.InferenceSession | None = None
        if _use_ort:
            self._ort_session = self._build_ort_session()
            if _use_compile:
                _log.info("use_ort=True: skipping torch.compile (ORT replaces it)")
                _use_compile = False

        # TRT replaces both torch.compile and ORT. Build from ONNX before compile.
        # Only supported on CUDA; silently disabled on cpu/mps.
        self._trt_context: Any | None = None
        if _use_trt:
            if self._device == "cuda":
                if _use_ort:
                    _log.info("use_trt=True: supersedes use_ort - ORT session will be unused")
                self._trt_context = self._build_trt_engine()
                if _use_compile:
                    _log.info("use_trt=True: skipping torch.compile (TRT replaces it)")
                    _use_compile = False
            else:
                _log.warning(
                    "use_trt=True ignored on device=%s (CUDA only); falling back to torch.compile",
                    self._device,
                )

        if _use_compile:
            try:
                if _compile_mode == "torch_tensorrt":
                    # TRT tactic selection via torch.compile backend.
                    # dynamic=False → torch specialises one TRT engine per exact batch
                    # shape seen during warmup, eliminating the dynamic-shape lookup
                    # overhead that regressed §3m. Each shape triggers ~25s TRT build
                    # on first call; the @modal.enter() warmup covers bs=1/8/24.
                    import torch_tensorrt as _trt_be  # noqa: F401 - registers backend

                    _prec = (
                        {torch.float16}
                        if self._use_fp16
                        else {torch.bfloat16}
                        if self._use_bf16
                        else {torch.float32}
                    )
                    self._model = torch.compile(  # type: ignore[assignment]
                        self._model,
                        backend="torch_tensorrt",
                        dynamic=False,
                        options={
                            "enabled_precisions": _prec,
                            "truncate_long_and_double": True,
                            "workspace_size": 4 << 30,
                            "min_block_size": 1,
                            "use_python_runtime": False,
                        },
                    )
                    _log.info("torch.compile applied (backend=torch_tensorrt prec=%s)", _prec)
                else:
                    self._model = torch.compile(self._model, mode=_compile_mode)  # type: ignore[assignment]
                    _log.info("torch.compile applied (mode=%s)", _compile_mode)
            except Exception as exc:
                _log.warning("torch.compile failed, falling back to uncompiled model: %s", exc)

        # Load the attribute heads from the config-driven directory. The
        # heads are a cheap matmul applied to the same L2-normalised embedding
        # below - one backbone pass yields both embedding and attributes.
        self._heads = AttributeHeads.load(resolve_attribute_heads_dir(config))

    def _build_ort_session(self) -> ort.InferenceSession:
        """Export the model to ONNX and return an ORT InferenceSession.

        Loads a separate eager-attention copy of the model for tracing - SDPA uses
        scaled_dot_product_attention which is not reliably traceable by the legacy
        torch.onnx.export API. The export model is discarded after writing the file.
        The ONNX file is cached in /tmp so repeated VitEmbedder instantiations in the
        same container reuse it without re-exporting.
        """
        import onnxruntime as _ort

        onnx_path = pathlib.Path(tempfile.gettempdir()) / "avsa_vit.onnx"

        if not onnx_path.exists():
            export_model = AutoModel.from_pretrained(_MODEL_NAME, attn_implementation="eager")
            export_model.eval()
            if self._use_fp16:
                export_model = export_model.half()
            elif self._use_bf16:
                export_model = export_model.bfloat16()
            export_model = export_model.to(self._device)

            dtype = (
                torch.float16
                if self._use_fp16
                else torch.bfloat16
                if self._use_bf16
                else torch.float32
            )
            dummy = torch.zeros(1, 3, 224, 224, dtype=dtype, device=self._device)

            with torch.no_grad():
                torch.onnx.export(
                    export_model,
                    {"pixel_values": dummy},
                    str(onnx_path),
                    input_names=["pixel_values"],
                    output_names=["last_hidden_state"],
                    dynamic_axes={"pixel_values": {0: "batch"}, "last_hidden_state": {0: "batch"}},
                    opset_version=17,
                )
            del export_model
            _log.info("ONNX model exported to %s", onnx_path)

        providers = (
            [("CUDAExecutionProvider", {"device_id": 0}), "CPUExecutionProvider"]
            if self._device == "cuda"
            else ["CPUExecutionProvider"]
        )
        opts = _ort.SessionOptions()
        opts.graph_optimization_level = _ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        session = _ort.InferenceSession(str(onnx_path), sess_options=opts, providers=providers)
        _log.info("ORT InferenceSession ready (providers=%s)", session.get_providers())
        return session

    def _build_trt_engine(self) -> Any:
        """Export ViT to ONNX (eager attn), compile a TRT FP16 engine, return an execution context.

        Engine is cached at /tmp/avsa_vit_fp16.trt so repeated container starts skip the
        ~60s build step. The ONNX export uses attn_implementation="eager" - TRT's parser
        does not support SDPA's scaled_dot_product_attention op.

        Dynamic batch profiles cover min=1, opt=8, max=24 (matching the batcher window).
        Returns a trt.IExecutionContext bound to CUDA stream 0.
        """
        import tensorrt as _trt

        onnx_path = pathlib.Path(tempfile.gettempdir()) / "avsa_vit_trt.onnx"
        engine_path = pathlib.Path(tempfile.gettempdir()) / "avsa_vit_fp16.trt"

        if not onnx_path.exists():
            _log.info("TRT: exporting ONNX (eager attn) to %s", onnx_path)
            export_model = AutoModel.from_pretrained(_MODEL_NAME, attn_implementation="eager")
            export_model.eval()
            if self._use_fp16:
                export_model = export_model.half()
            elif self._use_bf16:
                export_model = export_model.bfloat16()
            export_model = export_model.to(self._device)
            dtype = (
                torch.float16
                if self._use_fp16
                else torch.bfloat16
                if self._use_bf16
                else torch.float32
            )
            dummy = torch.zeros(1, 3, 224, 224, dtype=dtype, device=self._device)
            with torch.no_grad():
                torch.onnx.export(
                    export_model,
                    {"pixel_values": dummy},
                    str(onnx_path),
                    input_names=["pixel_values"],
                    output_names=["last_hidden_state"],
                    dynamic_axes={"pixel_values": {0: "batch"}, "last_hidden_state": {0: "batch"}},
                    opset_version=17,
                )
            del export_model
            _log.info("TRT: ONNX exported")

        if not engine_path.exists():
            _log.info("TRT: building FP16 engine (this takes ~60s) …")
            logger = _trt.Logger(_trt.Logger.WARNING)
            builder = _trt.Builder(logger)
            # TRT 10 makes EXPLICIT_BATCH the default (flag removed); TRT 8/9 need it.
            _eb = getattr(_trt, "NetworkDefinitionCreationFlag", None)
            _has_eb = _eb is not None and hasattr(_eb, "EXPLICIT_BATCH")
            network_flags = (1 << int(_eb.EXPLICIT_BATCH)) if _has_eb else 0
            network = builder.create_network(network_flags)
            parser = _trt.OnnxParser(network, logger)
            with open(onnx_path, "rb") as f:
                if not parser.parse(f.read()):
                    for idx in range(parser.num_errors):
                        _log.error("TRT parse error %d: %s", idx, parser.get_error(idx))
                    raise RuntimeError("TRT ONNX parse failed - see logs above")

            config = builder.create_builder_config()
            config.set_flag(_trt.BuilderFlag.FP16)
            # 4 GB workspace - enough for ViT-B/16 with all tactic variants.
            config.set_memory_pool_limit(_trt.MemoryPoolType.WORKSPACE, 4 << 30)

            profile = builder.create_optimization_profile()
            # (batch, channels, H, W) - dynamic batch axis only.
            profile.set_shape(
                "pixel_values",
                min=(1, 3, 224, 224),
                opt=(8, 3, 224, 224),
                max=(24, 3, 224, 224),
            )
            config.add_optimization_profile(profile)

            serialized = builder.build_serialized_network(network, config)
            if serialized is None:
                raise RuntimeError("TRT engine build returned None - check GPU/TRT version")
            with open(engine_path, "wb") as f:
                f.write(serialized)
            _log.info("TRT: engine saved to %s", engine_path)

        _log.info("TRT: deserializing engine from %s", engine_path)
        runtime = _trt.Runtime(_trt.Logger(_trt.Logger.WARNING))
        with open(engine_path, "rb") as f:
            engine = runtime.deserialize_cuda_engine(f.read())
        context = engine.create_execution_context()
        _log.info("TRT: execution context ready")
        return context

    @property
    def device(self) -> str:
        """The torch device the model is resident on (e.g. "cpu", "mps").

        Public accessor for the resolved device; the classification-overhead
        probe records it alongside the QPS/latency numbers.
        """
        return self._device

    def embed(self, images: list[bytes]) -> list[list[float]]:
        """Decode images, run the backbone, return CLS embeddings (768-d, L2-normalised).

        This is the **embedding-only** path: it runs the backbone forward and the
        L2-normalisation but does NOT apply the attribute heads. It is the
        backbone-only reference for the classification QPS-overhead measurement
: timing this against :meth:embed_with_attributes - which
        adds only the per-vector head matmul on the same backbone output -
        isolates the head-compute cost.
        """
        return self._backbone_embed(images)

    def embed_with_attributes(
        self, images: list[bytes]
    ) -> tuple[list[list[float]], list[AttributePrediction]]:
        """Run the backbone once; return embeddings and parallel attributes.

        The 768-d L2-normalised embedding is byte-for-byte the same as
        :meth:embed (same backbone pass, same normalisation); the attributes
        are the heads applied to that same embedding - a cheap per-vector
        matmul, not a second backbone pass.
        """
        embeddings = self._backbone_embed(images)
        attributes = self._heads.predict_batch(embeddings)
        return embeddings, attributes

    def _backbone_embed(self, images: list[bytes]) -> list[list[float]]:
        """The shared backbone forward → CLS → L2-normalise (no heads applied)."""
        pil_images = [Image.open(io.BytesIO(img)).convert("RGB") for img in images]
        if self._use_v2_preprocess:
            pixel_values = torch.stack([self._tv_preprocess(img) for img in pil_images])
            inputs = {"pixel_values": pixel_values}
        else:
            inputs = self._extractor(images=pil_images, return_tensors="pt")

        # Move inputs onto the model's device, then (in fp16) cast the float
        # tensors to half so dtype/device match the weights.
        inputs = {
            k: v.to(self._device) if isinstance(v, torch.Tensor) else v for k, v in inputs.items()
        }
        if self._use_fp16:
            inputs = {
                k: v.half() if isinstance(v, torch.Tensor) and v.dtype == torch.float32 else v
                for k, v in inputs.items()
            }
        if self._use_bf16:
            inputs = {
                k: v.bfloat16() if isinstance(v, torch.Tensor) and v.dtype == torch.float32 else v
                for k, v in inputs.items()
            }

        if self._trt_context is not None:
            # TensorRT inference (TRT 10 API): set dynamic batch shape via named
            # tensor addressing, execute async on the current CUDA stream, then
            # synchronize. All tensors stay on GPU until the final .tolist().
            pv = inputs["pixel_values"].contiguous()
            batch = pv.shape[0]
            self._trt_context.set_input_shape("pixel_values", (batch, 3, 224, 224))

            out_tensor = torch.empty(
                (batch, 197, 768),
                dtype=pv.dtype,
                device=self._device,
            )
            self._trt_context.set_tensor_address("pixel_values", pv.data_ptr())
            self._trt_context.set_tensor_address("last_hidden_state", out_tensor.data_ptr())
            stream = torch.cuda.current_stream().cuda_stream
            self._trt_context.execute_async_v3(stream)
            torch.cuda.synchronize()

            cls_vectors = out_tensor[:, 0, :].float()
            norms = cls_vectors.norm(dim=1, keepdim=True).clamp(min=1e-12)
            return (cls_vectors / norms).cpu().tolist()

        if self._ort_session is not None:
            # ORT IOBinding path: bind the CUDA tensor directly to ORT (no H2D copy)
            # and retrieve output via DLPack (no D2H copy)
            import numpy as np

            pv = inputs["pixel_values"].contiguous()
            elem_type = np.float16 if self._use_fp16 else np.float32
            if self._use_bf16:
                pv = pv.float()  # numpy has no bf16
            device_id = int(pv.device.index or 0)

            iob = self._ort_session.io_binding()
            iob.bind_input(
                name="pixel_values",
                device_type="cuda",
                device_id=device_id,
                element_type=elem_type,
                shape=tuple(pv.shape),
                buffer_ptr=pv.data_ptr(),
            )
            iob.bind_output("last_hidden_state", device_type="cuda")
            self._ort_session.run_with_iobinding(iob)

            # Zero-copy: OrtValue → torch tensor via DLPack (output stays on GPU)
            last_hidden_state = torch.from_dlpack(iob.get_outputs()[0].to_dlpack())
            cls_vectors = last_hidden_state[:, 0, :].float()
            norms = cls_vectors.norm(dim=1, keepdim=True).clamp(min=1e-12)
            return (cls_vectors / norms).cpu().tolist()

        # inference_mode is stricter than no_grad (also skips view/version
        # tracking)
        with torch.inference_mode():
            outputs = self._model(**inputs)

        # CLS token (batch, 768): normalise on-device in one batched divide, then
        # move to CPU once
        cls_vectors = outputs.last_hidden_state[:, 0, :].float()
        norms = cls_vectors.norm(dim=1, keepdim=True).clamp(min=1e-12)
        return (cls_vectors / norms).cpu().tolist()
