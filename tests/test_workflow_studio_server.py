import contextlib
import http.client
import io
import json
import os
import socket
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts.workflow_engine.studio_server import MAX_JSON_BODY, StudioConfig, create_server


ROOT = Path(__file__).resolve().parents[1]


class WorkflowStudioServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "project"
        self.assets = self.root / "assets"
        self.skills = self.root / "skills"
        self.project.mkdir()
        self.assets.mkdir()
        self.skills.mkdir()
        self.assets.joinpath("index.html").write_text("<main>studio</main>", encoding="utf-8")
        skill = self.skills / "test-workflow-task"
        skill.mkdir()
        skill.joinpath("SKILL.md").write_text(
            "---\nname: test-workflow-task\ndescription: A test skill\n---\nDo a test.\n",
            encoding="utf-8",
        )
        self.environment = mock.patch.dict(
            os.environ, {"CODEX_HOME": str(self.root / "codex-home")}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.config = StudioConfig(
            project_root=self.project,
            asset_root=self.assets,
            host="127.0.0.1",
            port=0,
            session_token="s" * 64,
            csrf_token="c" * 64,
            idle_timeout_seconds=30,
            open_browser=False,
            skill_roots=(self.skills,),
        )
        self.server, self.thread = self.start_server(self.config)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"

    def start_server(self, config):
        server = create_server(config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def tearDown(self):
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, *, body=None, raw_body=None, headers=None, port=None):
        target_port = self.port if port is None else port
        connection = http.client.HTTPConnection("127.0.0.1", target_port, timeout=5)
        request_headers = {"Authorization": "Bearer " + "s" * 64}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        payload = raw_body
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
        try:
            connection.request(method, path, body=payload, headers=request_headers)
            response = connection.getresponse()
            response_body = response.read()
            response_headers = {name.lower(): value for name, value in response.getheaders()}
        finally:
            connection.close()
        try:
            decoded = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            decoded = response_body
        return response.status, decoded, response_headers

    def write_headers(self):
        return {
            "Origin": self.origin,
            "X-Workflow-CSRF": "c" * 64,
            "Content-Type": "application/json",
        }

    @staticmethod
    def valid_workflow():
        workflow = json.loads(
            (ROOT / "tests" / "fixtures" / "workflow_valid_linear.json")
            .read_text(encoding="utf-8")
        )
        for node in workflow["nodes"]:
            node["skill_ref"] = "test-workflow-task"
        return workflow

    def request_without_content_length(self, method, path, headers, *, extra_headers=()):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        lines = [
            f"{method} {path} HTTP/1.1",
            f"Host: 127.0.0.1:{self.port}",
            "Authorization: Bearer " + "s" * 64,
        ]
        lines.extend(f"{name}: {value}" for name, value in headers.items())
        lines.extend(extra_headers)
        lines.extend(("Connection: close", "", ""))
        sock.sendall("\r\n".join(lines).encode("ascii"))
        response = http.client.HTTPResponse(sock)
        response.begin()
        body = response.read()
        sock.close()
        return response.status, json.loads(body.decode("utf-8"))

    def test_bootstrap_requires_session_token(self):
        status, data, _ = self.request(
            "GET", "/api/bootstrap", headers={"Authorization": "Bearer wrong"}
        )
        self.assertEqual(status, 401)
        self.assertEqual(data["errors"][0]["code"], "http.invalid_session")

    def test_non_ascii_session_and_csrf_headers_return_bounded_errors(self):
        status, data, _ = self.request(
            "GET", "/api/bootstrap", headers={"Authorization": "Bearer \u00e9" * 32}
        )
        self.assertEqual(status, 401)
        self.assertEqual(data["errors"][0]["code"], "http.invalid_session")

        status, data, _ = self.request(
            "POST",
            "/api/deactivate",
            body={},
            headers={**self.write_headers(), "X-Workflow-CSRF": "\u00e9" * 64},
        )
        self.assertEqual(status, 403)
        self.assertEqual(data["errors"][0]["code"], "http.invalid_csrf")

    def test_api_requires_exact_loopback_host(self):
        status, data, _ = self.request(
            "GET", "/api/bootstrap", headers={"Host": f"localhost:{self.port}"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(data["errors"][0]["code"], "http.invalid_host")

        status, data = self.request_without_content_length(
            "GET",
            "/api/bootstrap",
            {},
            extra_headers=(f"Host: 127.0.0.1:{self.port}",),
        )
        self.assertEqual(status, 403)
        self.assertEqual(data["errors"][0]["code"], "http.invalid_host")

    def test_get_routes_are_read_only_without_custom_selection(self):
        custom_state = self.project / ".research" / "custom-workflow"
        for route in ("/api/bootstrap", "/api/catalog", "/api/projection", "/api/workflow"):
            with self.subTest(route=route):
                status, data, _ = self.request("GET", route)
                self.assertEqual(status, 200, data)
                self.assertEqual(data["status"], "pass")
                self.assertFalse(custom_state.exists())
        self.assertEqual(data["data"]["workflow"], None)

    def test_bootstrap_returns_mode_csrf_and_request_limit(self):
        status, data, _ = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(data["data"]["mode"], "official")
        self.assertIsNone(data["data"]["active_workflow"])
        self.assertEqual(data["data"]["csrf_token"], "c" * 64)
        self.assertEqual(data["data"]["max_json_body_bytes"], MAX_JSON_BODY)

    def test_catalog_exposes_only_root_relative_skill_metadata(self):
        status, data, _ = self.request("GET", "/api/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "pass")
        skill = data["data"]["skills"][0]
        self.assertEqual(skill["catalog_id"], "test-workflow-task")
        self.assertEqual(skill["relative_path"], "test-workflow-task")
        self.assertNotIn(str(self.skills), json.dumps(data))
        validators = {item["validator_id"]: item for item in data["data"]["validators"]}
        self.assertIn("contract", validators["experiment-contract"]["input_roles"])
        self.assertIn("paper_type", validators["paper-section"]["options"])
        self.assertFalse(validators["humanizer-preflight"]["available"])

    def test_catalog_reports_installed_skill_locked_only_while_receipt_matches(self):
        from scripts.workflow_engine.catalog import tree_sha256

        skill = self.skills / "test-workflow-task"
        receipt = {
            "schema_version": "paper-workflow-install-v1", "profile": "core",
            "skills": {"test-workflow-task": {
                "tree_hash": tree_sha256(skill),
                "source": {"name": "test-workflow-task", "source": "bundled",
                           "path": "companion-skills/test-workflow-task", "license": "MIT"},
            }},
        }
        receipt_path = self.skills / ".paper-workflow-install.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        status, data, _ = self.request("GET", "/api/catalog")
        self.assertEqual(status, 200)
        self.assertTrue(data["data"]["skills"][0]["locked"])
        self.assertNotIn("catalog.unlocked_skill", {item["code"] for item in data["warnings"]})

        receipt["skills"]["test-workflow-task"]["tree_hash"] = "sha256:" + "0" * 64
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        status, data, _ = self.request("GET", "/api/catalog")
        self.assertEqual(status, 200)
        self.assertFalse(data["data"]["skills"][0]["locked"])
        self.assertIn("catalog.unlocked_skill", {item["code"] for item in data["warnings"]})

    def test_catalog_marks_ambiguous_skill_without_exposing_absolute_paths(self):
        duplicate_root = self.root / "duplicate-skills"
        duplicate = duplicate_root / "test-workflow-task"
        duplicate.mkdir(parents=True)
        duplicate.joinpath("SKILL.md").write_text(
            "---\nname: test-workflow-task\ndescription: Different identity\n---\n",
            encoding="utf-8",
        )
        config = StudioConfig(
            project_root=self.project,
            asset_root=self.assets,
            session_token="s" * 64,
            csrf_token="c" * 64,
            open_browser=False,
            skill_roots=(self.skills, duplicate_root),
        )
        server, thread = self.start_server(config)
        try:
            status, data, _ = self.request(
                "GET", "/api/catalog", port=server.server_address[1]
            )
            self.assertEqual(status, 200)
            self.assertEqual(data["status"], "pass")
            ambiguous = [item for item in data["data"]["skills"] if item["ambiguous"]]
            self.assertEqual([item["catalog_id"] for item in ambiguous], ["test-workflow-task"])
            self.assertIn("catalog.ambiguous_skill", {item["code"] for item in data["warnings"]})
            self.assertNotIn(str(self.root), json.dumps(data))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_validate_compile_save_activate_and_deactivate_use_server_owned_draft(self):
        workflow = self.valid_workflow()
        status, validation, _ = self.request(
            "POST", "/api/validate", body={"workflow": workflow}, headers=self.write_headers()
        )
        self.assertEqual(status, 200, validation)
        self.assertEqual(validation["status"], "pass", validation["errors"])
        self.assertRegex(validation["data"]["semantic_sha256"], r"^[0-9a-f]{64}$")

        status, preview, _ = self.request(
            "POST", "/api/compile", body={"workflow": workflow}, headers=self.write_headers()
        )
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["status"], "pass")
        self.assertEqual(preview["data"]["topological_order"], ["directions", "design"])
        self.assertFalse((self.project / ".research" / "custom-workflow").exists())

        status, saved, _ = self.request(
            "PUT",
            "/api/workflow",
            body={"workflow": workflow, "expected_document_revision": 0},
            headers=self.write_headers(),
        )
        self.assertEqual(status, 200, saved)
        warning_codes = validation["data"]["required_warning_codes"]
        activation_request = {
            "workflow_id": workflow["workflow_id"],
            "expected_document_revision": saved["data"]["document_revision"],
            "semantic_sha256": validation["data"]["semantic_sha256"],
            "acknowledged_warning_codes": [],
        }
        status, blocked, _ = self.request(
            "POST", "/api/activate", body=activation_request, headers=self.write_headers()
        )
        self.assertEqual(status, 409)
        self.assertEqual(blocked["errors"][0]["code"], "activation.acknowledgement_mismatch")
        self.assertFalse((self.project / ".research" / "custom-workflow" / "active-run").exists())
        status, unchanged, _ = self.request("GET", "/api/workflow")
        self.assertEqual(status, 200)
        self.assertEqual(unchanged["data"]["document_revision"], 1)

        activation_request["acknowledged_warning_codes"] = warning_codes
        status, activated, _ = self.request(
            "POST",
            "/api/activate",
            body=activation_request,
            headers=self.write_headers(),
        )
        self.assertEqual(status, 200, activated)
        self.assertEqual(activated["status"], "pass", activated["errors"])
        self.assertEqual(activated["data"]["document_revision"], 1)
        self.assertTrue((self.project / ".research" / "custom-workflow" / "active-run" / "state.json").exists())

        status, active, _ = self.request("GET", "/api/bootstrap")
        self.assertEqual((status, active["data"]["mode"]), (200, "custom"))
        self.assertEqual(active["data"]["active_workflow"]["workflow_id"], workflow["workflow_id"])
        self.assertEqual(active["data"]["active_workflow"]["semantic_sha256"], validation["data"]["semantic_sha256"])
        self.assertEqual(active["data"]["active_workflow"]["run_id"], activated["data"]["run_id"])
        status, deactivated, _ = self.request(
            "POST", "/api/deactivate", body={}, headers=self.write_headers()
        )
        self.assertEqual(status, 200, deactivated)
        status, official, _ = self.request("GET", "/api/bootstrap")
        self.assertEqual((status, official["data"]["mode"]), (200, "official"))
        self.assertIsNone(official["data"]["active_workflow"])
        self.assertTrue((self.project / ".research" / "custom-workflow" / "active-run" / "state.json").exists())

    def test_activation_rejects_client_supplied_workflow_data(self):
        status, data, _ = self.request(
            "POST",
            "/api/activate",
            body={"workflow": {}, "workflow_id": "flow"},
            headers=self.write_headers(),
        )
        self.assertEqual(status, 400)
        self.assertEqual(data["errors"][0]["code"], "http.invalid_request")

    def test_failed_run_start_restores_official_selection(self):
        workflow = self.valid_workflow()
        status, validation, _ = self.request(
            "POST", "/api/validate", body={"workflow": workflow}, headers=self.write_headers()
        )
        self.assertEqual(status, 200)
        status, saved, _ = self.request(
            "PUT",
            "/api/workflow",
            body={"workflow": workflow, "expected_document_revision": 0},
            headers=self.write_headers(),
        )
        self.assertEqual(status, 200)
        activation_request = {
            "workflow_id": workflow["workflow_id"],
            "expected_document_revision": saved["data"]["document_revision"],
            "semantic_sha256": validation["data"]["semantic_sha256"],
            "acknowledged_warning_codes": validation["data"]["required_warning_codes"],
        }
        with mock.patch.object(
            self.server.application.service.store,
            "_start_run_unlocked",
            side_effect=OSError("injected run-start failure"),
        ):
            status, failed, _ = self.request(
                "POST", "/api/activate", body=activation_request,
                headers=self.write_headers(),
            )

        self.assertEqual(status, 500, failed)
        self.assertEqual(failed["errors"][0]["code"], "activation.start_failed")
        self.assertTrue(failed["wrote_files"])
        self.assertIn("Official mode was restored", failed["errors"][0]["recovery"])
        status, bootstrap, _ = self.request("GET", "/api/bootstrap")
        self.assertEqual((status, bootstrap["data"]["mode"]), (200, "official"))
        self.assertFalse(
            (self.project / ".research" / "custom-workflow" / "active-run" / "state.json").exists()
        )

    def test_failed_run_start_after_state_write_stops_partial_activation(self):
        workflow = self.valid_workflow()
        status, validation, _ = self.request(
            "POST", "/api/validate", body={"workflow": workflow}, headers=self.write_headers()
        )
        self.assertEqual(status, 200)
        status, saved, _ = self.request(
            "PUT",
            "/api/workflow",
            body={"workflow": workflow, "expected_document_revision": 0},
            headers=self.write_headers(),
        )
        self.assertEqual(status, 200)
        activation_request = {
            "workflow_id": workflow["workflow_id"],
            "expected_document_revision": saved["data"]["document_revision"],
            "semantic_sha256": validation["data"]["semantic_sha256"],
            "acknowledged_warning_codes": validation["data"]["required_warning_codes"],
        }
        store = self.server.application.service.store
        original_start = store._start_run_unlocked

        def write_then_fail(prepared):
            original_start(prepared)
            raise OSError("injected failure after durable run start")

        with mock.patch.object(store, "_start_run_unlocked", side_effect=write_then_fail):
            status, failed, _ = self.request(
                "POST", "/api/activate", body=activation_request,
                headers=self.write_headers(),
            )

        self.assertEqual(status, 500, failed)
        self.assertEqual(failed["errors"][0]["code"], "activation.start_failed")
        self.assertTrue(failed["wrote_files"])
        status, bootstrap, _ = self.request("GET", "/api/bootstrap")
        self.assertEqual((status, bootstrap["data"]["mode"]), (200, "official"))
        state = json.loads(
            (self.project / ".research" / "custom-workflow" / "active-run" / "state.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(state["run_status"], "stopped")

    def test_failed_selector_projection_write_restores_official_selection(self):
        workflow = self.valid_workflow()
        status, validation, _ = self.request(
            "POST", "/api/validate", body={"workflow": workflow}, headers=self.write_headers()
        )
        self.assertEqual(status, 200)
        status, saved, _ = self.request(
            "PUT",
            "/api/workflow",
            body={"workflow": workflow, "expected_document_revision": 0},
            headers=self.write_headers(),
        )
        self.assertEqual(status, 200)
        activation_request = {
            "workflow_id": workflow["workflow_id"],
            "expected_document_revision": saved["data"]["document_revision"],
            "semantic_sha256": validation["data"]["semantic_sha256"],
            "acknowledged_warning_codes": validation["data"]["required_warning_codes"],
        }
        store = self.server.application.service.store
        original_atomic_json = store._atomic_json
        failure = {"pending": True}

        def fail_selection_projection_once(path, value):
            if path == store.paths.selection and failure["pending"]:
                failure["pending"] = False
                raise OSError("injected selector projection failure")
            return original_atomic_json(path, value)

        with mock.patch.object(store, "_atomic_json", side_effect=fail_selection_projection_once):
            status, failed, _ = self.request(
                "POST", "/api/activate", body=activation_request,
                headers=self.write_headers(),
            )

        self.assertEqual(status, 500, failed)
        self.assertEqual(failed["errors"][0]["code"], "activation.start_failed")
        self.assertTrue(failed["wrote_files"])
        self.assertIn("Official mode was restored", failed["errors"][0]["recovery"])
        status, bootstrap, _ = self.request("GET", "/api/bootstrap")
        self.assertEqual((status, bootstrap["data"]["mode"]), (200, "official"))
        self.assertFalse(
            (self.project / ".research" / "custom-workflow" / "active-run" / "state.json").exists()
        )
        audit_events = store.read_audit_events()
        self.assertEqual(audit_events[-1].event_type, "selection_deactivated")
        self.assertEqual(
            audit_events[-1].payload["selection"]["mode"], "official"
        )

    def test_state_change_requires_exact_origin_csrf_and_json(self):
        missing_origin = self.write_headers()
        missing_origin.pop("Origin")
        status, data, _ = self.request(
            "PUT", "/api/workflow", body={}, headers=missing_origin
        )
        self.assertEqual(status, 403)
        self.assertEqual(data["errors"][0]["code"], "http.origin_required")

        wrong_origin = self.write_headers()
        wrong_origin["Origin"] = self.origin + "/"
        status, data, _ = self.request(
            "PUT", "/api/workflow", body={}, headers=wrong_origin
        )
        self.assertEqual(status, 403)
        self.assertEqual(data["errors"][0]["code"], "http.invalid_origin")

        missing_csrf = self.write_headers()
        missing_csrf.pop("X-Workflow-CSRF")
        status, data, _ = self.request(
            "PUT", "/api/workflow", body={}, headers=missing_csrf
        )
        self.assertEqual(status, 403)
        self.assertEqual(data["errors"][0]["code"], "http.invalid_csrf")

        wrong_type = self.write_headers()
        wrong_type["Content-Type"] = "text/plain"
        status, data, _ = self.request(
            "PUT", "/api/workflow", body={}, headers=wrong_type
        )
        self.assertEqual(status, 415)
        self.assertEqual(data["errors"][0]["code"], "http.invalid_content_type")

    def test_state_change_requires_content_length_and_valid_json(self):
        headers = self.write_headers()
        status, data = self.request_without_content_length("POST", "/api/validate", headers)
        self.assertEqual(status, 411)
        self.assertEqual(data["errors"][0]["code"], "http.content_length_required")

        status, data = self.request_without_content_length(
            "POST",
            "/api/validate",
            headers,
            extra_headers=("Content-Length: " + "9" * 5000,),
        )
        self.assertEqual(status, 413)
        self.assertEqual(data["errors"][0]["code"], "http.body_too_large")

        status, data, _ = self.request(
            "POST", "/api/validate", raw_body=b"{broken", headers=headers
        )
        self.assertEqual(status, 400)
        self.assertEqual(data["errors"][0]["code"], "http.invalid_json")

        nested = b'{"workflow":' + b"[" * 1500 + b"0" + b"]" * 1500 + b"}"
        status, data, _ = self.request(
            "POST", "/api/validate", raw_body=nested, headers=headers
        )
        self.assertEqual(status, 400)
        self.assertEqual(data["errors"][0]["code"], "http.invalid_json")

    def test_oversized_json_body_is_rejected_before_processing(self):
        status, data, _ = self.request(
            "POST",
            "/api/validate",
            headers={**self.write_headers(), "Content-Length": str(MAX_JSON_BODY + 1)},
        )
        self.assertEqual(status, 413)
        self.assertEqual(data["errors"][0]["code"], "http.body_too_large")

    def test_unknown_api_route_has_stable_json_error(self):
        status, data, _ = self.request("GET", "/api/not-a-route")
        self.assertEqual(status, 404)
        self.assertEqual(data["errors"][0]["code"], "http.route_not_found")
        self.assertEqual(
            set(data["errors"][0]),
            {"code", "message", "operation", "recovery", "node_id", "edge_id"},
        )

    def test_static_paths_reject_traversal_and_symlinks(self):
        status, _data, _ = self.request("GET", "/%2e%2e/secret.txt")
        self.assertEqual(status, 404)
        self.assertFalse((self.project / ".research" / "custom-workflow").exists())

        nested = self.assets / "scripts"
        nested.mkdir()
        nested.joinpath("studio.js").write_text("window.studio = true;", encoding="utf-8")
        status, data, _ = self.request("GET", "/scripts/studio.js")
        self.assertEqual(status, 200)
        self.assertEqual(data, b"window.studio = true;")

        if os.name == "nt":
            self.skipTest("symlink creation may require elevated Windows privileges")
        external = self.root / "external.html"
        external.write_text("external content", encoding="utf-8")
        self.assets.joinpath("linked.html").symlink_to(external)
        status, _data, _ = self.request("GET", "/linked.html")
        self.assertEqual(status, 404)

        external_dir = self.root / "external-assets"
        external_dir.mkdir()
        external_dir.joinpath("secret.js").write_text("private", encoding="utf-8")
        self.assets.joinpath("linked-directory").symlink_to(external_dir, target_is_directory=True)
        status, _data, _ = self.request("GET", "/linked-directory/secret.js")
        self.assertEqual(status, 404)

    def test_static_response_has_security_headers_and_no_store(self):
        status, data, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(data, b"<main>studio</main>")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        self.assertIn("default-src 'self'", headers["content-security-policy"])
        self.assertNotIn("http://", headers["content-security-policy"])

    def test_two_tabs_cannot_overwrite_a_newer_draft_revision(self):
        workflow = json.loads(
            (ROOT / "tests" / "fixtures" / "workflow_valid_linear.json")
            .read_text(encoding="utf-8")
        )
        request = {"workflow": workflow, "expected_document_revision": 0}
        status, saved, _ = self.request(
            "PUT", "/api/workflow", body=request, headers=self.write_headers()
        )
        self.assertEqual(status, 200, saved)
        self.assertTrue(saved["wrote_files"])
        self.assertEqual(saved["data"]["document_revision"], 1)

        status, conflict, _ = self.request(
            "PUT", "/api/workflow", body=request, headers=self.write_headers()
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["errors"][0]["code"], "store.revision_conflict")

    def test_activation_serializes_with_a_concurrent_tab_save(self):
        workflow = self.valid_workflow()
        status, validation, _ = self.request(
            "POST", "/api/validate", body={"workflow": workflow}, headers=self.write_headers()
        )
        self.assertEqual(status, 200)
        status, saved, _ = self.request(
            "PUT",
            "/api/workflow",
            body={"workflow": workflow, "expected_document_revision": 0},
            headers=self.write_headers(),
        )
        self.assertEqual(status, 200)

        entered_validation = threading.Event()
        resume_activation = threading.Event()
        original_validate = self.server.application.service.validate_document

        def pause_activation_validation(document):
            entered_validation.set()
            if not resume_activation.wait(timeout=5):
                raise TimeoutError("test activation did not resume")
            return original_validate(document)

        activation_request = {
            "workflow_id": workflow["workflow_id"],
            "expected_document_revision": saved["data"]["document_revision"],
            "semantic_sha256": validation["data"]["semantic_sha256"],
            "acknowledged_warning_codes": validation["data"]["required_warning_codes"],
        }
        changed = json.loads(json.dumps(workflow))
        changed["ui"]["positions"]["directions"]["x"] += 20
        results = {}
        with mock.patch.object(
            self.server.application.service,
            "validate_document",
            side_effect=pause_activation_validation,
        ):
            activation_thread = threading.Thread(
                target=lambda: results.setdefault(
                    "activation",
                    self.request(
                        "POST", "/api/activate", body=activation_request,
                        headers=self.write_headers(),
                    ),
                )
            )
            activation_thread.start()
            self.assertTrue(entered_validation.wait(timeout=5))

            save_thread = threading.Thread(
                target=lambda: results.setdefault(
                    "save",
                    self.request(
                        "PUT", "/api/workflow",
                        body={"workflow": changed, "expected_document_revision": 1},
                        headers=self.write_headers(),
                    ),
                )
            )
            save_thread.start()
            time.sleep(0.05)
            resume_activation.set()
            activation_thread.join(timeout=5)
            save_thread.join(timeout=5)

        self.assertFalse(activation_thread.is_alive())
        self.assertFalse(save_thread.is_alive())
        self.assertEqual(results["activation"][0], 200, results["activation"][1])
        self.assertEqual(results["activation"][1]["status"], "pass")
        self.assertEqual(results["save"][0], 200, results["save"][1])
        self.assertEqual(results["save"][1]["data"]["document_revision"], 2)

    def test_activation_rejects_a_draft_changed_by_an_external_writer(self):
        from scripts.workflow_manager import WorkflowService

        workflow = self.valid_workflow()
        status, validation, _ = self.request(
            "POST", "/api/validate", body={"workflow": workflow}, headers=self.write_headers()
        )
        self.assertEqual(status, 200)
        status, saved, _ = self.request(
            "PUT",
            "/api/workflow",
            body={"workflow": workflow, "expected_document_revision": 0},
            headers=self.write_headers(),
        )
        self.assertEqual(status, 200)

        entered_validation = threading.Event()
        resume_activation = threading.Event()
        original_validate = self.server.application.service.validate_document

        def pause_validation(document):
            entered_validation.set()
            if not resume_activation.wait(timeout=5):
                raise TimeoutError("test activation did not resume")
            return original_validate(document)

        activation_request = {
            "workflow_id": workflow["workflow_id"],
            "expected_document_revision": saved["data"]["document_revision"],
            "semantic_sha256": validation["data"]["semantic_sha256"],
            "acknowledged_warning_codes": validation["data"]["required_warning_codes"],
        }
        changed = json.loads(json.dumps(workflow))
        changed["ui"]["positions"]["directions"]["x"] += 20
        results = {}
        with mock.patch.object(
            self.server.application.service,
            "validate_document",
            side_effect=pause_validation,
        ):
            activation_thread = threading.Thread(
                target=lambda: results.setdefault(
                    "activation",
                    self.request(
                        "POST", "/api/activate", body=activation_request,
                        headers=self.write_headers(),
                    ),
                )
            )
            activation_thread.start()
            self.assertTrue(entered_validation.wait(timeout=5))
            external_service = WorkflowService(self.project, skill_roots=(self.skills,))
            latest = external_service.save_draft(changed, expected_document_revision=1)
            self.assertEqual(latest["document_revision"], 2)
            resume_activation.set()
            activation_thread.join(timeout=5)

        self.assertFalse(activation_thread.is_alive())
        self.assertEqual(results["activation"][0], 409, results["activation"][1])
        self.assertEqual(
            results["activation"][1]["errors"][0]["code"], "store.revision_conflict"
        )
        status, current, _ = self.request("GET", "/api/workflow")
        self.assertEqual(status, 200)
        self.assertEqual(current["data"]["document_revision"], 2)
        self.assertEqual(
            current["data"]["workflow"]["ui"]["positions"]["directions"]["x"],
            changed["ui"]["positions"]["directions"]["x"],
        )
        self.assertFalse(
            (self.project / ".research" / "custom-workflow" / "active-run" / "state.json").exists()
        )

    def test_activation_and_external_deactivation_share_one_store_lock(self):
        workflow = self.valid_workflow()
        status, validation, _ = self.request(
            "POST", "/api/validate", body={"workflow": workflow}, headers=self.write_headers()
        )
        self.assertEqual(status, 200)
        status, saved, _ = self.request(
            "PUT",
            "/api/workflow",
            body={"workflow": workflow, "expected_document_revision": 0},
            headers=self.write_headers(),
        )
        self.assertEqual(status, 200)
        activation_request = {
            "workflow_id": workflow["workflow_id"],
            "expected_document_revision": saved["data"]["document_revision"],
            "semantic_sha256": validation["data"]["semantic_sha256"],
            "acknowledged_warning_codes": validation["data"]["required_warning_codes"],
        }

        entered_start = threading.Event()
        resume_start = threading.Event()
        deactivation_started = threading.Event()
        deactivation_done = threading.Event()
        store = self.server.application.service.store
        original_start = store._start_run_unlocked

        def pause_start(prepared):
            entered_start.set()
            if not resume_start.wait(timeout=5):
                raise TimeoutError("test activation did not resume")
            return original_start(prepared)

        results = {}
        with mock.patch.object(store, "_start_run_unlocked", side_effect=pause_start):
            activation_thread = threading.Thread(
                target=lambda: results.setdefault(
                    "activation",
                    self.request(
                        "POST", "/api/activate", body=activation_request,
                        headers=self.write_headers(),
                    ),
                )
            )
            activation_thread.start()
            self.assertTrue(entered_start.wait(timeout=5))

            def deactivate_externally():
                from scripts.workflow_manager import WorkflowService

                deactivation_started.set()
                result = WorkflowService(
                    self.project, skill_roots=(self.skills,)
                ).deactivate()
                results["deactivation"] = result
                deactivation_done.set()

            deactivation_thread = threading.Thread(target=deactivate_externally)
            deactivation_thread.start()
            self.assertTrue(deactivation_started.wait(timeout=5))
            self.assertFalse(deactivation_done.wait(timeout=0.1))
            resume_start.set()
            activation_thread.join(timeout=5)
            deactivation_thread.join(timeout=5)

        self.assertFalse(activation_thread.is_alive())
        self.assertFalse(deactivation_thread.is_alive())
        self.assertEqual(results["activation"][0], 200, results["activation"][1])
        self.assertEqual(results["deactivation"]["selection"]["mode"], "official")
        state = json.loads(
            (self.project / ".research" / "custom-workflow" / "active-run" / "state.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(state["run_status"], "stopped")

    def test_explicit_shutdown_stops_the_server_after_responding(self):
        status, data, _ = self.request(
            "POST", "/api/shutdown", body={}, headers=self.write_headers()
        )
        self.assertEqual(status, 200, data)
        self.assertTrue(data["data"]["shutdown_requested"])
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def test_server_close_waits_for_an_in_flight_draft_write(self):
        workflow = self.valid_workflow()
        entered_save = threading.Event()
        resume_save = threading.Event()
        close_finished = threading.Event()
        original_save = self.server.application.service.save_draft

        def pause_save(document, *, expected_document_revision):
            entered_save.set()
            if not resume_save.wait(timeout=5):
                raise TimeoutError("test draft save did not resume")
            return original_save(
                document, expected_document_revision=expected_document_revision
            )

        results = {}
        with mock.patch.object(
            self.server.application.service, "save_draft", side_effect=pause_save
        ):
            save_thread = threading.Thread(
                target=lambda: results.setdefault(
                    "save",
                    self.request(
                        "PUT",
                        "/api/workflow",
                        body={"workflow": workflow, "expected_document_revision": 0},
                        headers=self.write_headers(),
                    ),
                )
            )
            save_thread.start()
            self.assertTrue(entered_save.wait(timeout=5))
            status, shutdown, _ = self.request(
                "POST", "/api/shutdown", body={}, headers=self.write_headers()
            )
            self.assertEqual(status, 200, shutdown)
            self.thread.join(timeout=2)
            self.assertFalse(self.thread.is_alive())

            close_thread = threading.Thread(
                target=lambda: (self.server.server_close(), close_finished.set())
            )
            close_thread.start()
            self.assertFalse(close_finished.wait(timeout=0.1))
            resume_save.set()
            save_thread.join(timeout=5)
            close_thread.join(timeout=5)

        self.assertFalse(save_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertTrue(close_finished.is_set())
        self.assertEqual(results["save"][0], 200, results["save"][1])
        self.assertTrue(results["save"][1]["wrote_files"])

    def test_idle_timeout_stops_an_unused_server(self):
        config = StudioConfig(
            project_root=self.project,
            asset_root=self.assets,
            host="127.0.0.1",
            port=0,
            session_token="i" * 64,
            csrf_token="d" * 64,
            idle_timeout_seconds=0.15,
            open_browser=False,
            skill_roots=(self.skills,),
        )
        server, thread = self.start_server(config)
        thread.join(timeout=2)
        try:
            self.assertFalse(thread.is_alive())
        finally:
            if thread.is_alive():
                server.shutdown()
            server.server_close()

    def test_idle_timeout_closes_a_client_that_never_sends_a_request(self):
        config = StudioConfig(
            project_root=self.project,
            asset_root=self.assets,
            session_token="k" * 64,
            csrf_token="f" * 64,
            idle_timeout_seconds=0.25,
            open_browser=False,
            skill_roots=(self.skills,),
        )
        server, thread = self.start_server(config)
        server.request_timeout_seconds = 0.12
        connection = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=2)
        try:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), "stalled client prevented idle shutdown")
            self.assertEqual(server._active_requests, 0)
            connection.settimeout(1)
            self.assertEqual(connection.recv(1), b"")
        finally:
            connection.close()
            if thread.is_alive():
                server.shutdown()
            server.server_close()

    def test_absolute_request_deadline_closes_slow_drip_headers_and_body(self):
        config = StudioConfig(
            project_root=self.project,
            asset_root=self.assets,
            session_token="l" * 64,
            csrf_token="g" * 64,
            idle_timeout_seconds=30,
            open_browser=False,
            skill_roots=(self.skills,),
        )
        server, thread = self.start_server(config)
        server.request_timeout_seconds = 0.15
        port = server.server_address[1]

        def wait_for_active(expected):
            deadline = time.monotonic() + 2
            while server._active_requests != expected and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(server._active_requests, expected)

        def wait_for_close():
            deadline = time.monotonic() + 1
            while server._active_requests and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(server._active_requests, 0, "slow-drip client exceeded the total request deadline")

        def drip(connection, payload):
            for byte in payload:
                try:
                    connection.sendall(bytes((byte,)))
                except OSError:
                    return
                time.sleep(0.02)

        try:
            # An incomplete request line keeps producing data often enough to
            # defeat an inactivity timeout, but must still hit the total limit.
            headers_socket = socket.create_connection(("127.0.0.1", port), timeout=2)
            wait_for_active(1)
            header_writer = threading.Thread(
                target=drip, args=(headers_socket, b"G" * 80)
            )
            header_writer.start()
            wait_for_close()
            header_writer.join(timeout=1)
            headers_socket.close()

            # The same absolute limit applies after headers, while a bounded
            # JSON request body is arriving slowly.
            body_socket = socket.create_connection(("127.0.0.1", port), timeout=2)
            body = b"{" + b" " * 79 + b"}"
            request_headers = (
                "POST /api/shutdown HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Authorization: Bearer {'l' * 64}\r\n"
                f"Origin: http://127.0.0.1:{port}\r\n"
                f"X-Workflow-CSRF: {'g' * 64}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode("ascii")
            body_socket.sendall(request_headers)
            wait_for_active(1)
            body_writer = threading.Thread(target=drip, args=(body_socket, body))
            body_writer.start()
            wait_for_close()
            body_writer.join(timeout=1)
            body_socket.close()
        finally:
            if thread.is_alive():
                server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_server_close_finishes_after_a_stalled_client_times_out(self):
        server, thread = self.start_server(self.config)
        server.request_timeout_seconds = 0.12
        connection = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=2)
        close_finished = threading.Event()
        close_thread = None
        try:
            deadline = time.monotonic() + 2
            while server._active_requests == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(server._active_requests, 1, "test connection was not accepted")
            server.shutdown()
            close_thread = threading.Thread(
                target=lambda: (server.server_close(), close_finished.set())
            )
            close_thread.start()
            close_thread.join(timeout=2)
            self.assertFalse(close_thread.is_alive(), "server_close waited on a stalled client")
            self.assertTrue(close_finished.is_set())
            self.assertEqual(server._active_requests, 0)
        finally:
            connection.close()
            if thread.is_alive():
                server.shutdown()
            if close_thread is None:
                server.server_close()
            thread.join(timeout=2)

    def test_unauthenticated_static_requests_do_not_extend_idle_timeout(self):
        config = StudioConfig(
            project_root=self.project,
            asset_root=self.assets,
            session_token="j" * 64,
            csrf_token="e" * 64,
            idle_timeout_seconds=0.18,
            open_browser=False,
            skill_roots=(self.skills,),
        )
        server, thread = self.start_server(config)
        try:
            deadline = time.monotonic() + 0.35
            while time.monotonic() < deadline and thread.is_alive():
                try:
                    status, _data, _headers = self.request(
                        "GET", "/", headers={"Authorization": "Bearer invalid"},
                        port=server.server_address[1],
                    )
                    self.assertEqual(status, 200)
                except OSError:
                    break
                time.sleep(0.025)
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
        finally:
            if thread.is_alive():
                server.shutdown()
            server.server_close()


class WorkflowStudioLauncherTests(unittest.TestCase):
    def test_main_prints_one_json_start_record_and_uses_supplied_assets(self):
        from scripts.workflow_studio import main

        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project = root / "project"
            assets = root / "assets"
            project.mkdir()
            assets.mkdir()
            assets.joinpath("index.html").write_text("launcher test", encoding="utf-8")
            output = io.StringIO()
            environment = mock.patch.dict(os.environ, {"CODEX_HOME": str(root / "codex-home")})
            with environment, contextlib.redirect_stdout(output):
                result = main([
                    "--project", str(project), "--no-browser", "--idle-timeout", "0.1",
                    "--port", "0",
                ], asset_root_override=assets)
            self.assertEqual(result, 0)
            lines = output.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["status"], "ready")
            self.assertEqual(record["url"].split("#", 1)[0].startswith("http://127.0.0.1:"), True)
            self.assertIn("#session=", record["url"])

    def test_launcher_rejects_a_symlinked_project_root(self):
        from scripts.workflow_studio import main

        if os.name == "nt":
            self.skipTest("symlink creation may require elevated Windows privileges")
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / "project"
            target.mkdir()
            alias = root / "project-alias"
            alias.symlink_to(target, target_is_directory=True)
            with contextlib.redirect_stderr(io.StringIO()):
                result = main(["--project", str(alias), "--no-browser"], asset_root_override=root)
            self.assertEqual(result, 2)


if __name__ == "__main__":
    unittest.main()
