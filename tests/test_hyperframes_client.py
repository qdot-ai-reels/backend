import json
import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from app.hyperframes_client import HyperFramesClient, HyperFramesRenderError


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({"status": "completed", "job_id": "job-1"}).encode()


class HyperFramesClientTests(unittest.TestCase):
    @patch("app.hyperframes_client.request.urlopen", return_value=_Response())
    def test_posts_render_request_to_runner(self, urlopen):
        result = HyperFramesClient("http://hyperframes:8787").render("job-1")

        self.assertEqual(result["status"], "completed")
        sent_request = urlopen.call_args.args[0]
        self.assertEqual(sent_request.full_url, "http://hyperframes:8787/render")
        self.assertEqual(
            json.loads(sent_request.data),
            {"project_id": "job-1", "output_filename": "final.mp4"},
        )

    @patch("app.hyperframes_client.request.urlopen", side_effect=OSError)
    def test_wraps_runner_connection_error(self, _urlopen):
        with self.assertRaises(HyperFramesRenderError):
            HyperFramesClient("http://hyperframes:8787").render("job-1")

    @patch("app.hyperframes_client.request.urlopen")
    def test_includes_runner_error_detail(self, urlopen):
        urlopen.side_effect = HTTPError(
            "http://hyperframes:8787/render",
            502,
            "Bad Gateway",
            {},
            BytesIO(json.dumps({"message": "composition check failed"}).encode()),
        )

        with self.assertRaisesRegex(HyperFramesRenderError, "composition check failed"):
            HyperFramesClient("http://hyperframes:8787").render("job-1")


if __name__ == "__main__":
    unittest.main()
