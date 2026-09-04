import unittest

from app.main import _parse_cors_origins


class CorsOriginTests(unittest.TestCase):
    def test_localhost_adds_ipv4_loopback_alias(self) -> None:
        self.assertEqual(
            _parse_cors_origins("http://localhost:3000"),
            ["http://localhost:3000", "http://127.0.0.1:3000"],
        )

    def test_ipv4_loopback_adds_localhost_alias_without_duplicates(self) -> None:
        self.assertEqual(
            _parse_cors_origins(
                "http://127.0.0.1:3000,http://localhost:3000,http://127.0.0.1:3000"
            ),
            ["http://127.0.0.1:3000", "http://localhost:3000"],
        )

    def test_production_origin_is_not_broadened(self) -> None:
        self.assertEqual(
            _parse_cors_origins("https://studio.example.com,*, https://admin.example.com/"),
            ["https://studio.example.com", "https://admin.example.com"],
        )


if __name__ == "__main__":
    unittest.main()
