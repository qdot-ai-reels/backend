import unittest
from unittest.mock import patch
from unittest.mock import Mock

from app.api.v1.settings import (
    SettingsUpdateBody,
    get_google_tts_catalog,
    get_openrouter_catalog,
    get_settings,
    update_settings,
    list_google_tts_voices,
    list_openrouter_models,
)
from app.settings_service import (
    InMemorySettingsRepository,
    OpenRouterCatalogClient,
    GoogleTTSCatalogClient,
    SettingsService,
    SettingsValidationError,
)
from app.db import Base, SQLAlchemySettingsRepository, GlobalSettingsRow
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
