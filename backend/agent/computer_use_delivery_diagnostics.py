"""Private client-boundary image metadata; no provider payloads or model text."""
from __future__ import annotations

import base64
import hashlib
import io

from backend.computer_use.diagnostics import DiagnosticSink
from .computer_use_history import computer_use_provenance


class DeliveryDiagnostics:
    def __init__(self, service, session, messages, protected_urls, stage, run_scope):
        self.sink = None
        self.session = session
        self.run_scope = run_scope
        self.record = {}
        try:
            config = getattr(getattr(service, "config", None), "computer_use", None)
            if config is None or not config.diagnostics_enabled or session is None or session.is_child_context:
                return
            context = session.session_id
            latest = None
            for step in getattr(getattr(session.agent, "memory", None), "steps", []):
                for image in getattr(step, "observations_images", None) or []:
                    marker = computer_use_provenance(image, context)
                    if marker is not None:
                        latest = (image, marker)
            if latest is None:
                return
            image, marker = latest
            encoded = io.BytesIO()
            image.save(encoded, format="PNG")
            digest = hashlib.sha256(encoded.getvalue()).hexdigest()
            urls = [part.get("image_url", {}).get("url")
                    for message in messages if isinstance(message.content, list)
                    for part in message.content if isinstance(part, dict) and part.get("type") == "image_url"]
            trusted = [url for url in urls if url in protected_urls]
            matches = sum(hashlib.sha256(base64.b64decode(url.split(',', 1)[1], validate=True)).hexdigest() == digest
                          for url in trusted)
            self.record = dict(session_id=context, observation_id=marker.observation_id,
                geometry_version=marker.geometry_version, image_size=list(image.size), png_sha256=digest,
                prepared_image_count=len(urls), trusted_image_count=len(trusted),
                prepared_trusted_image_count=len(trusted), untrusted_image_count=len(urls)-len(trusted),
                latest_image_present=bool(matches), latest_image_match_count=matches,
                omitted_image_count=getattr(stage, "omitted_image_count", 0),
                omitted_block_count=getattr(stage, "omitted_block_count", 0))
            self.sink = DiagnosticSink(config)
        except Exception:
            # Diagnostics cannot change request validation or transport.
            self.sink = None

    def write(self, phase, kwargs, result, *, fallback=False, error_type=None):
        try:
            if self.sink is None:
                return
            self.sink.write(dict(self.record, phase=phase, operation="inference",
                request_id=kwargs.get("request_id"), stream=kwargs.get("stream", False),
                fallback=fallback, result=result, error_type=error_type))
        except Exception:
            pass

    def chat(self, service, kwargs, *, fallback=False):
        self.write("llm_request", kwargs, "submitted", fallback=fallback)
        try:
            result = service.chat(**kwargs)
        except BaseException as exc:
            self.write("llm_completed", kwargs, "error", fallback=fallback, error_type=type(exc).__name__)
            raise
        if not kwargs.get("stream"):
            self.write("llm_completed", kwargs, "returned", fallback=fallback)
            return result
        return self._stream(result, kwargs, fallback)

    def _stream(self, source, kwargs, fallback):
        outcome = "closed"
        try:
            for chunk in source:
                if chunk.get("aborted"):
                    outcome = "aborted"
                yield chunk
            if outcome != "aborted":
                outcome = "returned"
        except GeneratorExit:
            raise
        except BaseException as exc:
            outcome = "error"
            self.write("llm_completed", kwargs, outcome, fallback=fallback, error_type=type(exc).__name__)
            raise
        finally:
            if outcome != "error":
                self.write("llm_completed", kwargs, outcome, fallback=fallback)
            close = getattr(source, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass

    def accepted(self, kwargs, accepted, *, fallback=False):
        try:
            session = self.session
            if not accepted or session is None:
                return
            from backend.llm_service import is_request_aborted
            canceled = (getattr(session, "aborted", False) or getattr(session, "answer_now", False)
                        or getattr(session, "answering_now", False)
                        or is_request_aborted(kwargs.get("request_id")))
            if self.run_scope is not None:
                from .agent_runner import run_abort_manager
                canceled |= (run_abort_manager.is_aborted(self.run_scope)
                             or self.run_scope not in session.capture_active_run_scopes())
            self.write("llm_accepted", kwargs, "canceled" if canceled else "accepted", fallback=fallback)
        except Exception:
            pass
