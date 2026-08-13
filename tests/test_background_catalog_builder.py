from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image
import pytest

import object_storage_service
from scripts import build_background_catalog as builder


def save_test_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1024, 768), (215, 220, 205)).save(
        path,
        "JPEG",
        quality=92,
    )


def v14_generation_evidence(seed: int = 123456) -> dict[str, object]:
    return {
        "provider": "tencent-hunyuan",
        "providerAction": "TokenHubImageV3",
        "model": "hy-image-v3.0",
        "seed": seed,
        "requestedSeed": seed,
        "seedApplied": True,
        "promptRevisionEnabled": False,
        "promptRevisionControlApplied": True,
    }


def test_builder_default_plan_is_exactly_240_assets(capsys) -> None:
    assert builder.main([]) == 0

    output = capsys.readouterr().out
    assert '"categoryCount": 40' in output
    assert '"styleCount": 6' in output
    assert '"plannedAssetCount": 240' in output


def test_v12_builder_plan_is_isolated_to_six_mixed_rice_assets(capsys) -> None:
    with mock.patch.object(
        builder,
        "PROMPT_VERSION",
        builder.DEFAULT_PROMPT_VERSION,
    ):
        assert builder.main(
            [
                "--prompt-version",
                builder.background_profiles.MIXED_RICE_PILOT_PROMPT_VERSION,
                "--category",
                "mixed_rice",
            ]
        ) == 0

    output = capsys.readouterr().out
    assert '"promptVersion": "style-background.v12"' in output
    assert '"categoryCount": 1' in output
    assert '"styleCount": 6' in output
    assert '"plannedAssetCount": 6' in output


def test_v12_builder_refuses_a_multi_category_plan() -> None:
    with (
        mock.patch.object(
            builder,
            "PROMPT_VERSION",
            builder.DEFAULT_PROMPT_VERSION,
        ),
        pytest.raises(SystemExit, match="isolated mixed_rice pilot"),
    ):
        builder.main(
            [
                "--prompt-version",
                builder.background_profiles.MIXED_RICE_PILOT_PROMPT_VERSION,
            ]
        )


def test_v13_builder_plan_covers_the_complete_240_asset_catalog(capsys) -> None:
    with mock.patch.object(
        builder,
        "PROMPT_VERSION",
        builder.DEFAULT_PROMPT_VERSION,
    ):
        assert builder.main(
            [
                "--prompt-version",
                builder.background_profiles.BENCHMARKED_BACKGROUND_PROMPT_VERSION,
            ]
        ) == 0

    output = capsys.readouterr().out
    assert '"promptVersion": "style-background.v13"' in output
    assert '"categoryCount": 40' in output
    assert '"styleCount": 6' in output
    assert '"plannedAssetCount": 240' in output


def test_v14_builder_plan_covers_the_complete_240_asset_catalog(capsys) -> None:
    with mock.patch.object(
        builder,
        "PROMPT_VERSION",
        builder.DEFAULT_PROMPT_VERSION,
    ):
        assert builder.main(
            [
                "--prompt-version",
                builder.background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION,
            ]
        ) == 0

    output = capsys.readouterr().out
    assert '"promptVersion": "style-background.v14"' in output
    assert '"categoryCount": 40' in output
    assert '"styleCount": 6' in output
    assert '"plannedAssetCount": 240' in output


def test_execute_fails_fast_after_first_asset_failure(
    tmp_path: Path,
) -> None:
    with (
        mock.patch.object(builder.app_module, "tencent_ready", return_value=True),
        mock.patch.object(
            builder,
            "generate_entry",
            side_effect=RuntimeError("visual quality rejected"),
        ) as generate,
    ):
        result = builder.main(
            [
                "--execute",
                "--category",
                "mixed_rice",
                "--style",
                "style-1",
                "--style",
                "style-2",
                "--max-paid-calls",
                "2",
                "--output",
                str(tmp_path),
            ]
        )

    assert result == 1
    assert generate.call_count == 1
    report = builder.json.loads(
        (tmp_path / "run-report.json").read_text(encoding="utf-8")
    )
    assert report["failureCount"] == 1
    assert report["reservedPaidCalls"] == 1


def test_execute_stops_before_exceeding_paid_call_budget(
    tmp_path: Path,
) -> None:
    generated = {
        "categoryId": "mixed_rice",
        "styleId": "style-1",
        "sha256": "a" * 64,
        "reviewStatus": "pending",
    }
    with (
        mock.patch.object(builder.app_module, "tencent_ready", return_value=True),
        mock.patch.object(
            builder,
            "generate_entry",
            return_value=generated,
        ) as generate,
    ):
        result = builder.main(
            [
                "--execute",
                "--category",
                "mixed_rice",
                "--style",
                "style-1",
                "--style",
                "style-2",
                "--max-paid-calls",
                "1",
                "--output",
                str(tmp_path),
            ]
        )

    assert result == 1
    assert generate.call_count == 1
    report = builder.json.loads(
        (tmp_path / "run-report.json").read_text(encoding="utf-8")
    )
    assert report["completedAssetCount"] == 1
    assert report["failureCount"] == 1
    assert report["reservedPaidCalls"] == 1
    assert "paid call budget exhausted" in report["failures"][0]["error"]


def test_runtime_and_builder_share_the_v14_deterministic_seed() -> None:
    prompt_version = (
        builder.background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION
    )
    with mock.patch.object(builder, "PROMPT_VERSION", prompt_version):
        builder_seed = builder.deterministic_generation_seed(
            "mixed_rice",
            "style-4",
        )

    assert builder_seed == builder.app_module.deterministic_style_background_seed(
        "mixed_rice",
        "style-4",
        prompt_version,
    )


def test_v14_generation_fails_before_provider_without_tokenhub_v3(
    tmp_path: Path,
) -> None:
    target = tmp_path / "mixed_rice" / "style-1.jpg"
    prompt_version = (
        builder.background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION
    )
    with (
        mock.patch.object(builder, "PROMPT_VERSION", prompt_version),
        mock.patch.object(
            builder.app_module,
            "tokenhub_v3_deterministic_ready",
            return_value=False,
        ),
        mock.patch.object(builder.app_module, "tencent_api_request") as provider,
        pytest.raises(RuntimeError, match="requires TokenHub Hunyuan v3"),
    ):
        builder.generate_entry(
            category_id="mixed_rice",
            style_id="style-1",
            image_path=target,
            attempts=1,
        )

    provider.assert_not_called()


def test_v14_generation_omits_negative_prompt_and_requires_v3_action(
    tmp_path: Path,
) -> None:
    target = tmp_path / "mixed_rice" / "style-1.jpg"
    prompt_version = (
        builder.background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION
    )
    with mock.patch.object(builder, "PROMPT_VERSION", prompt_version):
        expected_seed = builder.deterministic_generation_seed(
            "mixed_rice",
            "style-1",
        )
    with (
        mock.patch.object(builder, "PROMPT_VERSION", prompt_version),
        mock.patch.object(
            builder.app_module,
            "tokenhub_v3_deterministic_ready",
            return_value=True,
        ),
        mock.patch.object(
            builder.app_module,
            "tencent_api_request",
            return_value={
                "ResultImage": "test-result",
                "_Provider": "tencent-hunyuan",
                "_Action": "TokenHubImageV3",
                "_Model": "hy-image-v3.0",
                "_SubmittedSeed": expected_seed,
                "_SubmittedRevise": 0,
                "RequestId": "request-v14",
            },
        ) as provider,
        mock.patch.object(
            builder.app_module,
            "save_result_image",
            side_effect=lambda _value, path: save_test_image(path),
        ),
        mock.patch.object(
            builder.app_module,
            "require_generated_background_quality",
            return_value={"status": "passed", "quality_score": 1.0},
        ),
    ):
        entry = builder.generate_entry(
            category_id="mixed_rice",
            style_id="style-1",
            image_path=target,
            attempts=1,
        )

    payload = provider.call_args.args[1]
    assert "NegativePrompt" not in payload
    assert payload["Revise"] == 0
    assert isinstance(payload["Seed"], int)
    assert entry["seed"] is None
    assert entry["seedApplied"] is False
    assert entry["seedControlSubmitted"] is True
    assert entry["seedEvidenceSource"] == "submitted-request"
    assert entry["providerSeedPresent"] is False
    assert entry["promptRevisionControlSubmitted"] is True
    assert entry["promptRevisionControlApplied"] is False


@pytest.mark.parametrize("returned_seed", [None, "123", 123.5, True, 999])
def test_v14_rejects_invalid_seed_evidence_before_downloading(
    tmp_path: Path,
    returned_seed: object,
) -> None:
    target = tmp_path / "mixed_rice" / "style-1.jpg"
    prompt_version = (
        builder.background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION
    )
    with (
        mock.patch.object(builder, "PROMPT_VERSION", prompt_version),
        mock.patch.object(
            builder.app_module,
            "tokenhub_v3_deterministic_ready",
            return_value=True,
        ),
        mock.patch.object(
            builder.app_module,
            "tencent_api_request",
            side_effect=lambda _action, payload: {
                "ResultImage": "test-result",
                "_Provider": "tencent-hunyuan",
                "_Action": "TokenHubImageV3",
                "_Model": "hy-image-v3.0",
                "Seed": returned_seed,
                "_SubmittedSeed": payload["Seed"],
                "_SubmittedRevise": payload["Revise"],
                "RequestId": "request-invalid-evidence",
            },
        ) as provider,
        mock.patch.object(builder.app_module, "save_result_image") as download,
        pytest.raises(RuntimeError, match="invalid Seed/Revise evidence"),
    ):
        builder.generate_entry(
            category_id="mixed_rice",
            style_id="style-1",
            image_path=target,
            attempts=3,
        )

    assert provider.call_count == 1
    download.assert_not_called()
    assert not target.exists()


@pytest.mark.parametrize("missing_field", ["_Provider", "_Model"])
def test_v14_rejects_missing_provider_identity_before_downloading(
    tmp_path: Path,
    missing_field: str,
) -> None:
    target = tmp_path / "mixed_rice" / "style-1.jpg"
    prompt_version = (
        builder.background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION
    )
    with mock.patch.object(builder, "PROMPT_VERSION", prompt_version):
        expected_seed = builder.deterministic_generation_seed(
            "mixed_rice",
            "style-1",
        )
    response = {
        "ResultImage": "test-result",
        "_Provider": "tencent-hunyuan",
        "_Action": "TokenHubImageV3",
        "_Model": "hy-image-v3.0",
        "Seed": expected_seed,
        "_SubmittedSeed": expected_seed,
        "_SubmittedRevise": 0,
        "RequestId": "request-missing-provider-identity",
    }
    response.pop(missing_field)

    with (
        mock.patch.object(builder, "PROMPT_VERSION", prompt_version),
        mock.patch.object(
            builder.app_module,
            "tokenhub_v3_deterministic_ready",
            return_value=True,
        ),
        mock.patch.object(
            builder.app_module,
            "tencent_api_request",
            return_value=response,
        ) as provider,
        mock.patch.object(builder.app_module, "save_result_image") as download,
        pytest.raises(RuntimeError, match="invalid Seed/Revise evidence"),
    ):
        builder.generate_entry(
            category_id="mixed_rice",
            style_id="style-1",
            image_path=target,
            attempts=3,
        )

    assert provider.call_count == 1
    download.assert_not_called()
    assert not target.exists()


def test_v14_result_download_failure_does_not_repeat_paid_generation(
    tmp_path: Path,
) -> None:
    target = tmp_path / "mixed_rice" / "style-2.jpg"
    prompt_version = (
        builder.background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION
    )
    with mock.patch.object(builder, "PROMPT_VERSION", prompt_version):
        expected_seed = builder.deterministic_generation_seed(
            "mixed_rice",
            "style-2",
        )
    with (
        mock.patch.object(builder, "PROMPT_VERSION", prompt_version),
        mock.patch.object(
            builder.app_module,
            "tokenhub_v3_deterministic_ready",
            return_value=True,
        ),
        mock.patch.object(
            builder.app_module,
            "tencent_api_request",
            return_value={
                "ResultImage": "https://cdn.example.test/result.jpg",
                "_Provider": "tencent-hunyuan",
                "_Action": "TokenHubImageV3",
                "_Model": "hy-image-v3.0",
                "_SubmittedSeed": expected_seed,
                "_SubmittedRevise": 0,
            },
        ) as provider,
        mock.patch.object(
            builder.app_module,
            "save_result_image",
            side_effect=builder.app_module.ProviderResultDownloadError(
                "download failed"
            ),
        ),
        pytest.raises(
            builder.app_module.ProviderResultDownloadError,
            match="download failed",
        ),
    ):
        builder.generate_entry(
            category_id="mixed_rice",
            style_id="style-2",
            image_path=target,
            attempts=3,
        )

    assert provider.call_count == 1


def test_v14_rejects_non_v3_result_without_downloading(
    tmp_path: Path,
) -> None:
    target = tmp_path / "mixed_rice" / "style-3.jpg"
    prompt_version = (
        builder.background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION
    )
    with (
        mock.patch.object(builder, "PROMPT_VERSION", prompt_version),
        mock.patch.object(
            builder.app_module,
            "tokenhub_v3_deterministic_ready",
            return_value=True,
        ),
        mock.patch.object(
            builder.app_module,
            "tencent_api_request",
            return_value={
                "ResultImage": "test-result",
                "_Action": "TextToImageLite",
            },
        ) as provider,
        mock.patch.object(builder.app_module, "save_result_image") as download,
        pytest.raises(RuntimeError, match="did not preserve deterministic"),
    ):
        builder.generate_entry(
            category_id="mixed_rice",
            style_id="style-3",
            image_path=target,
            attempts=3,
        )

    assert provider.call_count == 1
    download.assert_not_called()


def test_generate_entry_is_pending_and_prompt_bound(tmp_path: Path) -> None:
    target = tmp_path / "light_food" / "style-1.jpg"

    with (
        mock.patch.object(
            builder.app_module,
            "tencent_api_request",
            return_value={
                "ResultImage": "test-result",
                "_Provider": "tencent-hunyuan",
                "_Action": "TokenHubImageV3",
                "_Model": "hy-image-v3.0",
                "RequestId": "request-1",
            },
        ) as provider,
        mock.patch.object(
            builder.app_module,
            "save_result_image",
            side_effect=lambda _value, path: save_test_image(path),
        ),
        mock.patch.object(
            builder.app_module,
            "require_generated_background_quality",
            return_value={"status": "passed", "quality_score": 1.0},
        ),
    ):
        entry = builder.generate_entry(
            category_id="light_food",
            style_id="style-1",
            image_path=target,
            attempts=1,
        )

    assert entry["reviewStatus"] == "pending"
    assert entry["categoryId"] == "light_food"
    assert entry["styleId"] == "style-1"
    assert entry["promptVersion"] == "style-background.v11"
    assert entry["promptRevisionEnabled"] is None
    assert entry["promptRevisionControlApplied"] is False
    assert entry["seedApplied"] is False
    assert entry["seed"] is None
    assert entry["requestedSeed"] is None
    assert len(entry["promptSha256"]) == 64
    assert entry["width"] == 1024
    assert entry["height"] == 768
    assert entry["objectKey"].startswith(
        "ai-assets/waimai-shared/background-catalog/"
    )
    request_payload = provider.call_args.args[1]
    assert "Revise" not in request_payload
    assert "Seed" not in request_payload


def test_cloud_fallback_does_not_claim_seed_or_revision_control(
    tmp_path: Path,
) -> None:
    target = tmp_path / "light_food" / "style-1.jpg"

    with (
        mock.patch.object(
            builder.app_module,
            "tencent_api_request",
            return_value={
                "ResultImage": "test-result",
                "_Provider": "tencent-hunyuan",
                "_Action": "TextToImageLite",
                "_Model": "legacy-cloud",
                "RequestId": "request-cloud",
            },
        ),
        mock.patch.object(
            builder.app_module,
            "save_result_image",
            side_effect=lambda _value, path: save_test_image(path),
        ),
        mock.patch.object(
            builder.app_module,
            "require_generated_background_quality",
            return_value={"status": "passed", "quality_score": 1.0},
        ),
    ):
        entry = builder.generate_entry(
            category_id="light_food",
            style_id="style-1",
            image_path=target,
            attempts=1,
        )

    assert entry["requestedSeed"] is None
    assert entry["seed"] is None
    assert entry["seedApplied"] is False
    assert entry["promptRevisionEnabled"] is None
    assert entry["promptRevisionControlApplied"] is False


def test_generation_seed_is_stable_and_pair_scoped() -> None:
    seed = builder.deterministic_generation_seed("light_food", "style-1")

    assert seed == builder.deterministic_generation_seed(
        "light_food",
        "style-1",
    )
    assert seed != builder.deterministic_generation_seed(
        "light_food",
        "style-2",
    )
    assert 1 <= seed <= 4_294_967_295


def test_generation_retry_uses_stable_distinct_seed(tmp_path: Path) -> None:
    target = tmp_path / "hotpot_skewers" / "style-2.jpg"
    requested_seeds: list[int] = []

    def provider_request(
        _action: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        requested_seeds.append(int(payload["Seed"]))
        if len(requested_seeds) == 1:
            raise RuntimeError("FailedOperation.ImageIllegalDetected")
        return {
            "ResultImage": "test-result",
            "_Provider": "tencent-hunyuan",
            "_Action": "TokenHubImageV3",
            "_Model": "hy-image-v3.0",
            "_SubmittedSeed": requested_seeds[-1],
            "_SubmittedRevise": 0,
            "RequestId": "request-retry",
        }

    with (
        mock.patch.object(
            builder,
            "PROMPT_VERSION",
            builder.background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION,
        ),
        mock.patch.object(
            builder.app_module,
            "tokenhub_v3_deterministic_ready",
            return_value=True,
        ),
        mock.patch.object(
            builder.app_module,
            "tencent_api_request",
            side_effect=provider_request,
        ),
        mock.patch.object(
            builder.app_module,
            "save_result_image",
            side_effect=lambda _value, path: save_test_image(path),
        ),
        mock.patch.object(
            builder.app_module,
            "require_generated_background_quality",
            return_value={"status": "passed", "quality_score": 1.0},
        ),
        mock.patch.object(builder.time, "sleep"),
    ):
        entry = builder.generate_entry(
            category_id="hotpot_skewers",
            style_id="style-2",
            image_path=target,
            attempts=2,
        )

    with mock.patch.object(
        builder,
        "PROMPT_VERSION",
        builder.background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION,
    ):
        expected_seeds = [
            builder.deterministic_generation_seed(
                "hotpot_skewers",
                "style-2",
                attempt,
            )
            for attempt in (1, 2)
        ]
    assert requested_seeds == expected_seeds
    assert requested_seeds[0] != requested_seeds[1]
    assert entry["seed"] is None
    assert entry["requestedSeed"] == requested_seeds[1]
    assert entry["seedControlSubmitted"] is True


def test_generation_seed_revision_starts_from_next_deterministic_seed(
    tmp_path: Path,
) -> None:
    target = tmp_path / "pizza" / "style-2.jpg"
    with mock.patch.object(
        builder,
        "PROMPT_VERSION",
        builder.background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION,
    ):
        expected = builder.deterministic_generation_seed(
            "pizza",
            "style-2",
            2,
        )

    with (
        mock.patch.object(
            builder,
            "PROMPT_VERSION",
            builder.background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION,
        ),
        mock.patch.object(
            builder.app_module,
            "tokenhub_v3_deterministic_ready",
            return_value=True,
        ),
        mock.patch.object(
            builder.app_module,
            "tencent_api_request",
            return_value={
                "ResultImage": "test-result",
                "_Provider": "tencent-hunyuan",
                "_Action": "TokenHubImageV3",
                "_Model": "hy-image-v3.0",
                "_SubmittedSeed": expected,
                "_SubmittedRevise": 0,
                "RequestId": "request-revision",
            },
        ) as provider,
        mock.patch.object(
            builder.app_module,
            "save_result_image",
            side_effect=lambda _value, path: save_test_image(path),
        ),
        mock.patch.object(
            builder.app_module,
            "require_generated_background_quality",
            return_value={"status": "passed", "quality_score": 1.0},
        ),
    ):
        entry = builder.generate_entry(
            category_id="pizza",
            style_id="style-2",
            image_path=target,
            attempts=1,
            seed_revision=1,
        )

    assert provider.call_args.args[1]["Seed"] == expected
    assert entry["seed"] is None
    assert entry["requestedSeed"] == expected


@contextmanager
def dummy_postgres_connection():
    yield object()


class CapturingStore:
    calls: list[dict[str, object]] = []

    def __init__(self, _connection: object) -> None:
        pass

    def register_asset(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(dict(kwargs))
        return SimpleNamespace(
            created=True,
            record={
                "id": "asset-" + ("a" * 40),
                "status": "pending_review",
                "review_status": "pending",
            },
        )


def test_register_entry_writes_shared_private_object_pending(
    tmp_path: Path,
) -> None:
    target = tmp_path / "light_food" / "style-1.jpg"
    save_test_image(target)
    fingerprint = builder.app_module.image_file_fingerprint(target)
    prompt_version = (
        builder.background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION
    )
    prompt = builder.background_profiles.pure_background_prompt(
        "light_food",
        "style-1",
        prompt_version=prompt_version,
    )
    prompt_sha = builder.hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    entry = {
        "catalogVersion": builder.background_catalog.CATALOG_VERSION,
        "taxonomyVersion": builder.app_module.TAXONOMY_VERSION,
        "categoryId": "light_food",
        "categoryName": "轻食/沙拉",
        "styleId": "style-1",
        "styleSlotName": "暖色纯色棚拍",
        "promptVersion": prompt_version,
        "promptSha256": prompt_sha,
        **v14_generation_evidence(),
        "sha256": fingerprint["sha256"],
        "objectKey": builder.background_catalog.catalog_object_key(
            category_id="light_food",
            style_id="style-1",
            prompt_version=prompt_version,
            prompt_sha256=prompt_sha,
            asset_sha256=fingerprint["sha256"],
        ),
    }
    storage = object_storage_service.ObjectStorageService(
        tmp_path / "objects"
    )
    CapturingStore.calls = []

    with (
        mock.patch.object(
            builder.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
        mock.patch.object(
            builder.app_module,
            "postgres_connection",
            dummy_postgres_connection,
        ),
        mock.patch.object(
            builder.app_module.product_asset_library_store,
            "ProductAssetLibraryStore",
            CapturingStore,
        ),
    ):
        registered = builder.register_pending_entry(
            entry,
            target,
            actor_user_id="background-catalog-builder",
        )

    assert registered["registered"] is True
    assert registered["reviewStatus"] == "pending"
    assert storage.read_bytes(str(entry["objectKey"])) == target.read_bytes()
    assert len(CapturingStore.calls) == 1
    call = CapturingStore.calls[0]
    assert call["tenant_id"] == "waimai-shared"
    assert call["reuse_scope"] == "tenant"
    assert call["asset_kind"] == "background"
    assert call["category_id"] == "light_food"
    assert call["style_id"] == "style-1"
    assert call["prompt_version"] == "style-background.v14"
    assert call["pipeline_version"] == "style-background.v14"


def test_v14_local_reuse_requires_verified_generation_evidence(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "mixed_rice" / "style-1.jpg"
    sidecar_path = image_path.with_suffix(".json")
    save_test_image(image_path)
    fingerprint = builder.app_module.image_file_fingerprint(image_path)
    prompt_version = (
        builder.background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION
    )
    with mock.patch.object(builder, "PROMPT_VERSION", prompt_version):
        prompt = builder.background_prompt("mixed_rice", "style-1")
    prompt_sha = builder.hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    entry = {
        "catalogVersion": builder.background_catalog.CATALOG_VERSION,
        "taxonomyVersion": builder.app_module.TAXONOMY_VERSION,
        "categoryId": "mixed_rice",
        "styleId": "style-1",
        "promptVersion": prompt_version,
        "promptSha256": prompt_sha,
        "sha256": fingerprint["sha256"],
        **v14_generation_evidence(),
    }

    invalid = {
        **entry,
        "providerAction": "TextToImageLite",
        "seedApplied": False,
        "promptRevisionControlApplied": False,
    }
    sidecar_path.write_text(
        builder.json.dumps(invalid),
        encoding="utf-8",
    )
    with mock.patch.object(builder, "PROMPT_VERSION", prompt_version):
        assert builder.reusable_local_entry(
            image_path,
            sidecar_path,
            category_id="mixed_rice",
            style_id="style-1",
            prompt_sha256=prompt_sha,
        ) is None

        ambiguous_submitted = {
            **entry,
            "seed": None,
            "seedApplied": False,
            "seedEvidenceSource": "submitted-request",
            "providerSeedEchoed": False,
            "seedControlSubmitted": True,
            "promptRevisionControlSubmitted": True,
            "promptRevisionControlApplied": False,
        }
        sidecar_path.write_text(
            builder.json.dumps(ambiguous_submitted),
            encoding="utf-8",
        )
        assert builder.reusable_local_entry(
            image_path,
            sidecar_path,
            category_id="mixed_rice",
            style_id="style-1",
            prompt_sha256=prompt_sha,
        ) is None

        sidecar_path.write_text(
            builder.json.dumps(entry),
            encoding="utf-8",
        )
        assert builder.reusable_local_entry(
            image_path,
            sidecar_path,
            category_id="mixed_rice",
            style_id="style-1",
            prompt_sha256=prompt_sha,
        ) == entry


def test_v14_upload_rejects_asset_without_generation_evidence(
    tmp_path: Path,
) -> None:
    target = tmp_path / "mixed_rice" / "style-1.jpg"
    save_test_image(target)
    fingerprint = builder.app_module.image_file_fingerprint(target)
    entry = {
        "promptVersion": (
            builder.background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION
        ),
        "sha256": fingerprint["sha256"],
        "objectKey": "ai-assets/waimai-shared/background-catalog/rejected.jpg",
        "provider": "tencent-hunyuan",
        "providerAction": "TextToImageLite",
        "model": "legacy-cloud",
        "seedApplied": False,
        "promptRevisionControlApplied": False,
    }
    storage = object_storage_service.ObjectStorageService(
        tmp_path / "objects"
    )

    with (
        mock.patch.object(
            builder.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
        pytest.raises(RuntimeError, match="lacks verified TokenHub v3"),
    ):
        builder.upload_pending_entry(entry, target)

    assert not storage.exists(str(entry["objectKey"]))


@pytest.mark.parametrize(
    ("requested_seed", "applied_seed"),
    [
        (123.9, 123.1),
        ("123", 123),
        (123, "123"),
        (True, 1),
        (4_294_967_295.9, 4_294_967_295.1),
    ],
)
def test_v14_generation_evidence_requires_strict_integer_seeds(
    requested_seed: object,
    applied_seed: object,
) -> None:
    entry = {
        "promptVersion": (
            builder.background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION
        ),
        **v14_generation_evidence(),
        "requestedSeed": requested_seed,
        "seed": applied_seed,
    }

    assert builder.entry_generation_contract_valid(entry) is False


def test_v14_submitted_evidence_requires_explicit_seed_presence() -> None:
    entry = {
        "promptVersion": (
            builder.background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION
        ),
        "provider": "tencent-hunyuan",
        "providerAction": "TokenHubImageV3",
        "model": "hy-image-v3.0",
        "seed": None,
        "requestedSeed": 123456,
        "seedApplied": False,
        "seedEvidenceSource": "submitted-request",
        "providerSeedEchoed": False,
        "seedControlSubmitted": True,
        "promptRevisionEnabled": False,
        "promptRevisionControlSubmitted": True,
        "promptRevisionControlApplied": False,
    }

    assert builder.entry_generation_contract_valid(entry) is False


def test_v14_submitted_evidence_requires_presence_for_exact_echo() -> None:
    entry = {
        "promptVersion": (
            builder.background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION
        ),
        "provider": "tencent-hunyuan",
        "providerAction": "TokenHubImageV3",
        "model": "hy-image-v3.0",
        "seed": 123456,
        "requestedSeed": 123456,
        "seedApplied": True,
        "seedEvidenceSource": "submitted-request",
        "providerSeedEchoed": True,
        "seedControlSubmitted": True,
        "promptRevisionEnabled": False,
        "promptRevisionControlSubmitted": True,
        "promptRevisionControlApplied": False,
    }

    assert builder.entry_generation_contract_valid(entry) is False


def test_upload_pending_entry_writes_cos_object_without_registration(
    tmp_path: Path,
) -> None:
    target = tmp_path / "light_food" / "style-1.jpg"
    save_test_image(target)
    fingerprint = builder.app_module.image_file_fingerprint(target)
    prompt = builder.background_profiles.pure_background_prompt(
        "light_food",
        "style-1",
    )
    prompt_sha = builder.hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    entry = {
        "sha256": fingerprint["sha256"],
        "objectKey": builder.background_catalog.catalog_object_key(
            category_id="light_food",
            style_id="style-1",
            prompt_version=builder.PROMPT_VERSION,
            prompt_sha256=prompt_sha,
            asset_sha256=fingerprint["sha256"],
        ),
    }
    storage = object_storage_service.ObjectStorageService(
        tmp_path / "objects"
    )

    with mock.patch.object(
        builder.object_storage_service,
        "get_object_storage_service",
        return_value=storage,
    ):
        object_key, digest, existed = builder.upload_pending_entry(
            entry,
            target,
        )

    assert existed is False
    assert digest == fingerprint["sha256"]
    assert object_key == entry["objectKey"]
    assert storage.read_bytes(object_key) == target.read_bytes()

    with mock.patch.object(
        builder.object_storage_service,
        "get_object_storage_service",
        return_value=storage,
    ):
        replay = builder.upload_pending_entry(entry, target)
    assert replay == (object_key, digest, True)
