import unittest
from unittest.mock import patch
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.settings import (
    SettingsUpdateBody,
    get_settings,
    update_settings,
    list_google_tts_voices,
    list_openrouter_models,
    router,
    get_settings_repository,
    get_openrouter_catalog,
    get_google_tts_catalog,
)
from app.settings_service import (
    InMemorySettingsRepository,
    OpenRouterCatalogClient,
    GoogleTTSCatalogClient,
    SettingsService,
    SettingsValidationError,
)
from app.db import Base, SQLAlchemySettingsRepository, GlobalSettingsRow
from app.runtime_config import build_script_client, build_tts_settings, build_video_client
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import lifespan


class SettingsServiceTests(unittest.TestCase):
    def test_api_key_is_encrypted_and_never_returned(self):
        repository = InMemorySettingsRepository()
        service = SettingsService(repository, encryption_key=SettingsService.test_key())

        service.update({"openrouter_api_key": "sk-secret", "openrouter_model": "model-a"})
        stored = repository.get()

        self.assertNotEqual(stored.openrouter_api_key_encrypted, "sk-secret")
        self.assertTrue(service.get_public().api_key_configured)
        self.assertNotIn("sk-secret", str(service.get_public()))

    def test_update_keeps_existing_secret_when_key_is_omitted(self):
        repository = InMemorySettingsRepository()
        service = SettingsService(repository, encryption_key=SettingsService.test_key())

        service.update({"openrouter_api_key": "sk-secret"})
        service.update({"openrouter_model": "model-b"})

        self.assertEqual(service.get_openrouter_api_key(), "sk-secret")
        self.assertEqual(service.get_public().openrouter_model, "model-b")

    def test_rejects_invalid_video_duration(self):
        service = SettingsService(
            InMemorySettingsRepository(), encryption_key=SettingsService.test_key()
        )

        with self.assertRaises(SettingsValidationError):
            service.update({"video_max_duration_seconds": 31})

    def test_sqlalchemy_repository_persists_one_global_record(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        repository = SQLAlchemySettingsRepository(session)

        settings = repository.get()
        settings.openrouter_model = "model-a"
        repository.save(settings)

        self.assertEqual(repository.get().openrouter_model, "model-a")
        self.assertEqual(session.query(GlobalSettingsRow).count(), 1)

    def test_saved_settings_are_used_by_generation_clients(self):
        service = SettingsService(
            InMemorySettingsRepository(), encryption_key=SettingsService.test_key()
        )
        service.update(
            {
                "openrouter_api_key": "db-key",
                "openrouter_model": "db-script-model",
                "openrouter_video_model": "db-video-model",
                "google_tts_voice_name": "ko-KR-Wavenet-A",
                "video_max_duration_seconds": 20,
                "max_retries": 3,
            }
        )

        script_client = build_script_client(service)
        video_client = build_video_client(service)
        tts_settings = build_tts_settings(service)

        self.assertEqual(script_client.api_key, "db-key")
        self.assertEqual(script_client.model, "db-script-model")
        self.assertEqual(video_client.api_key, "db-key")
        self.assertEqual(video_client.model, "db-video-model")
        self.assertEqual(tts_settings.voice_name, "ko-KR-Wavenet-A")
        self.assertEqual(service.get_runtime_settings().video_max_duration_seconds, 20)
        self.assertEqual(service.get_runtime_settings().max_retries, 3)


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

    def test_google_tts_catalog_maps_voice_fields(self):
        voice = Mock(name="ko-KR-Standard-A")
        voice.name = "ko-KR-Standard-A"
        voice.language_codes = ["ko-KR"]
        voice.ssml_gender = "FEMALE"
        client = Mock()
        client.list_voices.return_value = Mock(voices=[voice])

        result = GoogleTTSCatalogClient(client=client).list_voices()

        self.assertEqual(
            result,
            [{"name": "ko-KR-Standard-A", "language_codes": ["ko-KR"], "ssml_gender": "FEMALE"}],
        )


class SettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.repository = InMemorySettingsRepository()
        self.service = SettingsService(
            self.repository, encryption_key=SettingsService.test_key()
        )

    def test_settings_put_and_get_do_not_expose_api_key(self):
        response = update_settings(
            SettingsUpdateBody(openrouter_api_key="sk-secret", openrouter_model="m1"),
            self.service,
        )
        self.assertNotIn("sk-secret", str(response))
        self.assertTrue(response["api_key_configured"])

        response = get_settings(self.service)
        self.assertEqual(response["openrouter_model"], "m1")

    def test_catalog_endpoints(self):
        openrouter = Mock(list_models=Mock(return_value=[{"id": "m1", "name": "M1"}]))
        google = Mock(list_voices=Mock(return_value=[{"name": "v1"}]))
        self.assertEqual(list_openrouter_models(openrouter), [{"id": "m1", "name": "M1"}])
        self.assertEqual(list_google_tts_voices(google), [{"name": "v1"}])


class DatabaseLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_initializes_database_once_during_application_startup(self):
        with patch("app.main.init_db") as init_db:
            async with lifespan(object()):
                pass

        init_db.assert_called_once_with()


class SettingsHttpTests(unittest.TestCase):
    def test_settings_routes_return_http_responses(self):
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        service = SettingsService(
            InMemorySettingsRepository(), encryption_key=SettingsService.test_key()
        )
        openrouter = Mock(list_models=Mock(return_value=[{"id": "m1", "name": "M1"}]))
        google = Mock(list_voices=Mock(return_value=[{"name": "v1"}]))
        app.dependency_overrides[get_settings_repository] = lambda: service
        app.dependency_overrides[get_openrouter_catalog] = lambda: openrouter
        app.dependency_overrides[get_google_tts_catalog] = lambda: google

        with TestClient(app) as client:
            update_response = client.put(
                "/api/v1/settings",
                json={"openrouter_api_key": "sk-secret", "openrouter_model": "m1"},
            )
            settings_response = client.get("/api/v1/settings")
            models_response = client.get("/api/v1/settings/openrouter/models")
            voices_response = client.get("/api/v1/settings/google-tts/voices")

        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(settings_response.status_code, 200)
        self.assertEqual(models_response.status_code, 200)
        self.assertEqual(voices_response.status_code, 200)
        self.assertNotIn("sk-secret", update_response.text)
        self.assertEqual(settings_response.json()["openrouter_model"], "m1")
        self.assertEqual(models_response.json(), [{"id": "m1", "name": "M1"}])
        self.assertEqual(voices_response.json(), [{"name": "v1"}])
