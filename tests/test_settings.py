import unittest
from unittest.mock import patch
from unittest.mock import Mock

from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.settings import (
    SettingsUpdateBody,
    get_settings,
    update_settings,
    list_openrouter_tts_voices,
    list_openrouter_tts_models,
    list_openrouter_models,
    router,
    get_settings_repository,
    get_openrouter_catalog,
    get_openrouter_tts_catalog,
    get_openrouter_video_catalog,
)
from app.settings_service import (
    InMemorySettingsRepository,
    OpenRouterCatalogClient,
    OpenRouterTTSCatalogClient,
    SettingsService,
    SettingsValidationError,
    OpenRouterVideoCatalogClient,
    VideoModelCapabilities,
)
from app.db import Base, SQLAlchemySettingsRepository, GlobalSettingsRow, init_db
from app.runtime_config import (
    build_script_client,
    build_tts_settings,
    build_video_client,
)
from app.tts_generator import OpenRouterTTSSettings
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.main import lifespan


class SettingsServiceTests(unittest.TestCase):
    def test_generation_clients_use_task_specific_environment_variables(self):
        with patch.dict(
            "os.environ",
            {
                "OPENROUTER_SCRIPT_API_KEY": "script-key",
                "OPENROUTER_SCRIPT_MODEL": "script-model",
                "OPENROUTER_TTS_API_KEY": "tts-key",
                "OPENROUTER_TTS_MODEL": "tts-model",
                "OPENROUTER_TTS_VOICE": "",
                "OPENROUTER_VIDEO_API_KEY": "video-key",
                "OPENROUTER_VIDEO_MODEL": "video-model",
            },
            clear=True,
        ):
            script_client = build_script_client()
            tts_settings = OpenRouterTTSSettings.from_env()
            video_client = build_video_client()

        self.assertEqual((script_client.api_key, script_client.model), ("script-key", "script-model"))
        self.assertEqual((tts_settings.api_key, tts_settings.model), ("tts-key", "tts-model"))
        self.assertEqual((video_client.api_key, video_client.model), ("video-key", "video-model"))

    def test_video_client_accepts_background_poll_limit(self):
        client = build_video_client(
            capabilities=VideoModelCapabilities(
                model_id="video-model",
                name="Video",
                supported_durations=(15,),
                supported_aspect_ratios=("9:16",),
                supported_resolutions=("720p",),
                generate_audio=False,
            ),
            max_poll_attempts=72,
        )

        self.assertEqual(client.max_poll_attempts, 72)

    def test_api_key_is_encrypted_and_never_returned(self):
        repository = InMemorySettingsRepository()
        service = SettingsService(repository, encryption_key=SettingsService.test_key())

        service.update({"openrouter_script_api_key": "sk-secret", "openrouter_script_model": "model-a"})
        stored = repository.get()

        self.assertNotEqual(stored.openrouter_script_api_key_encrypted, "sk-secret")
        self.assertTrue(service.get_public().script_api_key_configured)
        self.assertNotIn("sk-secret", str(service.get_public()))

    def test_update_keeps_existing_secret_when_key_is_omitted(self):
        repository = InMemorySettingsRepository()
        service = SettingsService(repository, encryption_key=SettingsService.test_key())

        service.update({"openrouter_script_api_key": "sk-secret"})
        service.update({"openrouter_script_model": "model-b"})

        self.assertEqual(service.get_script_api_key(), "sk-secret")
        self.assertEqual(service.get_public().openrouter_script_model, "model-b")

    def test_rejects_invalid_video_duration(self):
        service = SettingsService(
            InMemorySettingsRepository(), encryption_key=SettingsService.test_key()
        )

        with self.assertRaises(SettingsValidationError):
            service.update({"video_max_duration_seconds": 31})

    def test_rejects_invalid_resolution_range(self):
        service = SettingsService(
            InMemorySettingsRepository(), encryption_key=SettingsService.test_key()
        )

        with self.assertRaises(SettingsValidationError):
            service.update({"video_min_resolution": "1080p", "video_max_resolution": "720p"})
        with self.assertRaises(SettingsValidationError):
            service.update({"video_min_resolution": "full-hd"})

    def test_uses_prd_defaults(self):
        public = SettingsService(
            InMemorySettingsRepository(), encryption_key=SettingsService.test_key()
        ).get_public()

        self.assertEqual(public.video_min_resolution, "1080p")
        self.assertEqual(public.video_max_resolution, "1080p")
        self.assertEqual(public.video_max_duration_seconds, 15)
        self.assertEqual(public.script_generation_retries, 4)
        self.assertEqual(public.video_generation_retries, 2)
        self.assertEqual(public.media_combine_retries, 3)

    def test_sqlalchemy_repository_persists_one_global_record(self):
        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        self.addCleanup(session.close)
        repository = SQLAlchemySettingsRepository(session)

        settings = repository.get()
        settings.openrouter_script_model = "model-a"
        repository.save(settings)

        self.assertEqual(repository.get().openrouter_script_model, "model-a")
        self.assertEqual(session.query(GlobalSettingsRow).count(), 1)

    def test_saved_settings_are_used_by_generation_clients(self):
        service = SettingsService(
            InMemorySettingsRepository(), encryption_key=SettingsService.test_key()
        )
        service.update(
            {
                "openrouter_script_api_key": "script-db-key",
                "openrouter_tts_api_key": "tts-db-key",
                "openrouter_video_api_key": "video-db-key",
                "openrouter_script_model": "db-script-model",
                "openrouter_tts_model": "db-tts-model",
                "openrouter_video_model": "db-video-model",
                "openrouter_tts_voice": "test-voice",
                "video_max_duration_seconds": 20,
                "script_generation_retries": 2,
                "video_generation_retries": 1,
                "media_combine_retries": 3,
            }
        )

        script_client = build_script_client(service)
        video_client = build_video_client(service)
        tts_settings = build_tts_settings(service)

        self.assertEqual(script_client.api_key, "script-db-key")
        self.assertEqual(script_client.model, "db-script-model")
        self.assertEqual(video_client.api_key, "video-db-key")
        self.assertEqual(video_client.model, "db-video-model")
        self.assertEqual(tts_settings.model, "db-tts-model")
        self.assertEqual(tts_settings.voice_name, "test-voice")
        self.assertEqual(service.get_runtime_settings().video_max_duration_seconds, 20)
        self.assertEqual(service.get_runtime_settings().script_generation_retries, 2)
        self.assertEqual(service.get_runtime_settings().video_generation_retries, 1)
        self.assertEqual(service.get_runtime_settings().media_combine_retries, 3)

    def test_video_capabilities_are_used_without_database_settings(self):
        with patch.dict("os.environ", {"OPENROUTER_VIDEO_API_KEY": "env-key"}):
            capabilities = VideoModelCapabilities(
                model_id="video-model",
                name="Video Model",
                supported_durations=(5, 8),
                supported_aspect_ratios=("9:16",),
                supported_resolutions=("1080p",),
                generate_audio=False,
            )

            client = build_video_client(capabilities=capabilities)

        self.assertEqual(client.api_key, "env-key")
        self.assertEqual(client.supported_durations, (5, 8))
        self.assertEqual(client.supported_resolutions, ("1080p",))


class ProviderCatalogTests(unittest.TestCase):
    def test_openrouter_catalog_maps_model_fields(self):
        opener = Mock()
        response = Mock()
        response.read.return_value = b'{"data":[{"id":"model-a","name":"Model A","context_length":4096}]}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener.return_value = response

        result = OpenRouterCatalogClient("test-key", opener=opener).list_models()

        self.assertEqual(result, [{"id": "model-a", "name": "Model A", "context_length": 4096}])
        request = opener.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")

    def test_openrouter_tts_catalog_maps_model_voices(self):
        opener = Mock()
        response = Mock()
        response.read.return_value = (
            b'{"data":[{"id":"fish-audio/s2.1-pro-free:free",'
            b'"supported_voices":["voice-a"]}]}'
        )
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener.return_value = response

        result = OpenRouterTTSCatalogClient("test-key", opener=opener).list_voices()

        self.assertEqual(
            result,
            [{"model": "fish-audio/s2.1-pro-free:free", "name": "voice-a"}],
        )
        self.assertIn("output_modalities=speech", opener.call_args.args[0].full_url)

    def test_openrouter_video_catalog_maps_capabilities_and_endpoint(self):
        opener = Mock()
        response = Mock()
        response.read.return_value = (
            b'{"data":[{"id":"video-a","name":"Video A",'
            b'"supported_durations":[5,8],"supported_aspect_ratios":["9:16"],'
            b'"supported_resolutions":["720p"],"generate_audio":false}]}'
        )
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener.return_value = response

        result = OpenRouterVideoCatalogClient("test-key", opener=opener).list_models()

        self.assertEqual(result[0].model_id, "video-a")
        self.assertEqual(result[0].supported_durations, (5, 8))
        self.assertEqual(result[0].supported_aspect_ratios, ("9:16",))
        self.assertEqual(result[0].supported_resolutions, ("720p",))
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://openrouter.ai/api/v1/videos/models")


class SettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.repository = InMemorySettingsRepository()
        self.service = SettingsService(
            self.repository, encryption_key=SettingsService.test_key()
        )

    def test_settings_put_and_get_do_not_expose_api_key(self):
        response = update_settings(
            SettingsUpdateBody(openrouter_script_api_key="sk-secret", openrouter_script_model="m1"),
            self.service,
        )
        self.assertNotIn("sk-secret", str(response))
        self.assertTrue(response["script_api_key_configured"])

        response = get_settings(self.service)
        self.assertEqual(response["openrouter_script_model"], "m1")

    def test_update_rejects_unknown_video_model_when_catalog_is_available(self):
        catalog = OpenRouterVideoCatalogClient()
        catalog.list_models = Mock(
            return_value=[
                VideoModelCapabilities(
                    model_id="video-a",
                    name="Video A",
                    supported_durations=(5, 8),
                    supported_aspect_ratios=("9:16",),
                    supported_resolutions=("720p",),
                    generate_audio=False,
                )
            ]
        )

        with self.assertRaises(HTTPException) as context:
            update_settings(
                SettingsUpdateBody(openrouter_video_model="video-unknown"),
                self.service,
                catalog,
            )

        self.assertEqual(context.exception.status_code, 422)

    def test_adjusts_video_settings_to_selected_model_capabilities(self):
        catalog = OpenRouterVideoCatalogClient()
        catalog.list_models = Mock(
            return_value=[
                VideoModelCapabilities(
                    model_id="video-a",
                    name="Video A",
                    supported_durations=(4, 6, 8),
                    supported_aspect_ratios=("9:16",),
                    supported_resolutions=("720p",),
                    generate_audio=False,
                )
            ]
        )

        result = update_settings(
            SettingsUpdateBody(
                openrouter_video_model="video-a",
                video_min_resolution="720p",
                video_max_resolution="1080p",
                video_max_duration_seconds=15,
            ),
            self.service,
            catalog,
        )

        self.assertEqual(result["video_min_resolution"], "720p")
        self.assertEqual(result["video_max_resolution"], "1080p")
        self.assertEqual(result["video_max_duration_seconds"], 8)

    def test_catalog_endpoints(self):
        openrouter = Mock(list_models=Mock(return_value=[{"id": "m1", "name": "M1"}]))
        tts = Mock(
            list_voices=Mock(return_value=[{"name": "v1"}]),
            list_models=Mock(return_value=[{"id": "tts-1", "name": "TTS 1"}]),
        )
        self.assertEqual(list_openrouter_models(openrouter), [{"id": "m1", "name": "M1"}])
        self.assertEqual(list_openrouter_tts_voices(tts), [{"name": "v1"}])
        self.assertEqual(list_openrouter_tts_models(tts), [{"id": "tts-1", "name": "TTS 1"}])


class DatabaseLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_initializes_database_once_during_application_startup(self):
        with patch("app.main.init_db") as init_db:
            async with lifespan(object()):
                pass

        init_db.assert_called_once_with()

    def test_migrates_legacy_settings_columns(self):
        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE global_settings (
                        id INTEGER PRIMARY KEY,
                        openrouter_api_key_encrypted VARCHAR(1024),
                        openrouter_model VARCHAR(255),
                        openrouter_video_model VARCHAR(255),
                        google_tts_voice_name VARCHAR(255) NOT NULL DEFAULT 'ko-KR-Standard-A',
                        video_resolution VARCHAR(32) NOT NULL DEFAULT '720p',
                        video_max_duration_seconds INTEGER NOT NULL DEFAULT 30,
                        max_retries INTEGER NOT NULL DEFAULT 2,
                        mute_original_audio BOOLEAN NOT NULL DEFAULT 1,
                        updated_at DATETIME
                    )
                    """
                )
            )
            connection.execute(
                text(
                    "INSERT INTO global_settings "
                    "(id, video_resolution, video_max_duration_seconds, max_retries) "
                    "VALUES (1, '720p', 15, 4)"
                )
            )

        with patch("app.db.engine", engine):
            init_db()

        columns = {column["name"] for column in inspect(engine).get_columns("global_settings")}
        self.assertTrue(
            {
                "openrouter_script_model",
                "openrouter_tts_voice",
                "video_min_resolution",
                "video_max_resolution",
                "script_generation_retries",
                "video_generation_retries",
                "media_combine_retries",
            }.issubset(columns)
        )
        generation_indexes = {
            item["name"] for item in inspect(engine).get_indexes("generation_jobs")
        }
        self.assertTrue(
            {
                "ix_generation_jobs_created_at_job_id",
                "ix_generation_jobs_status_created_at_job_id",
            }.issubset(generation_indexes)
        )
        self.assertIn("generation_requests", inspect(engine).get_table_names())
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT video_min_resolution, video_max_resolution, "
                    "script_generation_retries, video_generation_retries, media_combine_retries "
                    "FROM global_settings WHERE id = 1"
                )
            ).one()
            self.assertEqual(tuple(row), ("720p", "720p", 4, 2, 3))


class SettingsHttpTests(unittest.TestCase):
    def test_settings_routes_return_http_responses(self):
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        service = SettingsService(
            InMemorySettingsRepository(), encryption_key=SettingsService.test_key()
        )
        openrouter = Mock(list_models=Mock(return_value=[{"id": "m1", "name": "M1"}]))
        tts = Mock(list_voices=Mock(return_value=[{"name": "v1"}]))
        app.dependency_overrides[get_settings_repository] = lambda: service
        app.dependency_overrides[get_openrouter_catalog] = lambda: openrouter
        app.dependency_overrides[get_openrouter_tts_catalog] = lambda: tts

        with TestClient(app) as client:
            update_response = client.put(
                "/api/v1/settings",
                json={"openrouter_script_api_key": "sk-secret", "openrouter_script_model": "m1"},
            )
            settings_response = client.get("/api/v1/settings")
            models_response = client.get("/api/v1/settings/openrouter/models")
            voices_response = client.get("/api/v1/settings/openrouter-tts/voices")

        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(settings_response.status_code, 200)
        self.assertEqual(models_response.status_code, 200)
        self.assertEqual(voices_response.status_code, 200)
        self.assertNotIn("sk-secret", update_response.text)
        self.assertEqual(settings_response.json()["openrouter_script_model"], "m1")
        self.assertEqual(models_response.json(), [{"id": "m1", "name": "M1"}])
        self.assertEqual(voices_response.json(), [{"name": "v1"}])
