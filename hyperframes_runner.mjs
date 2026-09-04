import http from "node:http";
import { randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import path from "node:path";
import { performance } from "node:perf_hooks";

const port = Number(process.env.HYPERFRAMES_RUNNER_PORT || 8787);
const workspace = path.resolve(process.env.HYPERFRAMES_WORKSPACE || "/workspace");

function sendJson(response, statusCode, payload) {
  response.writeHead(statusCode, { "content-type": "application/json" });
  response.end(JSON.stringify(payload));
}

function isInsideWorkspace(candidate) {
  const relative = path.relative(workspace, candidate);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function runHyperFrames(phase, args) {
  return new Promise((resolve, reject) => {
    const startedAt = performance.now();
    const child = spawn("hyperframes", args, { cwd: workspace });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", reject);
    child.on("close", (code) => {
      const durationMs = Math.round(performance.now() - startedAt);
      console.log(JSON.stringify({
        event: "hyperframes_command",
        phase,
        args,
        exitCode: code,
        durationMs,
        stdoutTail: stdout.trim().slice(-4000),
        stderrTail: stderr.trim().slice(-4000),
      }));
      if (code === 0) {
        resolve({ stdout, stderr });
      } else {
        const error = new Error(stderr.trim() || stdout.trim() || `HyperFrames exited with ${code}`);
        error.code = code;
        reject(error);
      }
    });
  });
}

async function readBody(request) {
  let body = "";
  for await (const chunk of request) body += chunk;
  if (!body) return {};
  return JSON.parse(body);
}

const server = http.createServer(async (request, response) => {
  if (request.method === "GET" && request.url === "/health") {
    sendJson(response, 200, { status: "healthy", version: "0.8.27" });
    return;
  }

  if (request.method !== "POST" || request.url !== "/render") {
    sendJson(response, 404, { error: "not_found" });
    return;
  }

  try {
    let body;
    try {
      body = await readBody(request);
    } catch {
      sendJson(response, 400, { error: "invalid_json" });
      return;
    }
    const projectId = String(body.project_id || "");
    const outputFilename = String(body.output_filename || "final.mp4");
    const projectPath = path.resolve(workspace, projectId);
    const outputPath = path.resolve(projectPath, outputFilename);

    if (!projectId || !isInsideWorkspace(projectPath) || !isInsideWorkspace(outputPath)) {
      sendJson(response, 400, { error: "invalid_workspace_path" });
      return;
    }

    const jobId = randomUUID();
    await runHyperFrames("check", ["check", projectPath, "--json", "--strict"]);
    const quality = String(process.env.HYPERFRAMES_RENDER_QUALITY || "high");
    const bitrate = String(process.env.HYPERFRAMES_VIDEO_BITRATE || "10M");
    await runHyperFrames("render", [
      "render", projectPath,
      "--output", outputPath,
      "--quality", quality,
      "--video-bitrate", bitrate,
      "--fps", "30",
      "--strict",
      "--workers", "1",
    ]);
    sendJson(response, 200, {
      job_id: jobId,
      status: "completed",
      output_path: outputPath,
    });
  } catch (error) {
    sendJson(response, 502, {
      error: "hyperframes_render_failed",
      message: error instanceof Error ? error.message : String(error),
    });
  }
});

server.listen(port, "0.0.0.0", () => {
  console.log(`HyperFrames runner listening on :${port}`);
});
