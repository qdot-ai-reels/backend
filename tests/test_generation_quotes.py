import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.generation_quotes import (
    GenerationQuoteExpiredError,
    GenerationQuoteMismatchError,
    QuoteSpec,
    create_generation_quote,
    validate_generation_quote,
)


class GenerationQuoteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "quotes.db"
        self.engine = create_engine(
            f"sqlite:///{path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.session_local = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
        )
        self.session_patch = patch(
            "app.generation_quotes.SessionLocal",
            self.session_local,
        )
        self.session_patch.start()
        self.now = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)
        self.spec = QuoteSpec(
            template_id="ugc_quick_4",
            template_version=1,
            duration_seconds=4,
            candidate_count=2,
            visual_mode="generated_model",
            resolution="1080p",
        )

    def tearDown(self):
        self.session_patch.stop()
        self.engine.dispose()
        self.directory.cleanup()

    def test_persists_default_rate_math_and_fifteen_minute_ttl(self):
        with patch.dict(
            "os.environ",
            {
                "VIDEO_RATE_PER_SECOND_USD": "0.38",
                "VIDEO_QUOTE_MIN_FACTOR": "0.95",
                "VIDEO_QUOTE_MAX_FACTOR": "1.10",
            },
        ):
            result = create_generation_quote(
                self.spec,
                model_id="bytedance/seedance-2.0",
                now=self.now,
            )

        self.assertEqual(result["line_items"][0]["quantity"], 8)
        self.assertEqual(result["line_items"][0]["unit_price_expected"], 0.38)
        self.assertEqual(
            result["total"],
            {"min": 2.888, "expected": 3.04, "max": 3.344},
        )
        self.assertEqual(
            result["total"]["max"],
            result["line_items"][0]["subtotal_max"],
        )
        self.assertEqual(result["coverage"], "video_only")
        self.assertEqual(result["automatic_paid_retries"], 0)
        self.assertEqual(
            datetime.fromisoformat(result["expires_at"]),
            self.now + timedelta(minutes=15),
        )
        self.assertIn("TTS", result["disclaimer"])

    def test_rejects_expired_quote(self):
        result = create_generation_quote(
            self.spec,
            model_id="bytedance/seedance-2.0",
            now=self.now,
        )

        with self.assertRaises(GenerationQuoteExpiredError):
            validate_generation_quote(
                result["quote_id"],
                self.spec,
                now=self.now + timedelta(minutes=15),
            )

    def test_rejects_generation_parameters_that_differ_from_quote(self):
        result = create_generation_quote(
            self.spec,
            model_id="bytedance/seedance-2.0",
            now=self.now,
        )
        changed = QuoteSpec(
            template_id=self.spec.template_id,
            template_version=self.spec.template_version,
            duration_seconds=self.spec.duration_seconds,
            candidate_count=3,
            visual_mode=self.spec.visual_mode,
            resolution=self.spec.resolution,
        )

        with self.assertRaises(GenerationQuoteMismatchError):
            validate_generation_quote(result["quote_id"], changed, now=self.now)

        with self.assertRaises(GenerationQuoteMismatchError):
            validate_generation_quote(
                result["quote_id"],
                self.spec,
                model_id="different/model",
                now=self.now,
            )


if __name__ == "__main__":
    unittest.main()
