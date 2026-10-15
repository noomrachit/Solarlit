import path from "node:path";
import { fileURLToPath } from "node:url";
import fs from "node:fs";
import express, { type Express } from "express";
import rateLimit from "express-rate-limit";
import cors from "cors";
import pinoHttp from "pino-http";
import session from "express-session";
import router from "./routes";
import { logger } from "./lib/logger";
const app: Express = express();

// Railway terminates TLS at its proxy: trust the first hop so req.secure,
// req.ip (rate limiting) and the `secure` session cookie work correctly.
app.set("trust proxy", 1);
app.disable("x-powered-by");

// Security headers on every response (no extra dependency needed).
app.use((_req, res, next) => {
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.setHeader("X-Frame-Options", "DENY");
  res.setHeader("Referrer-Policy", "strict-origin-when-cross-origin");
  res.setHeader("Permissions-Policy", "camera=(), microphone=(), geolocation=()");
  res.setHeader("Cross-Origin-Opener-Policy", "same-origin");
  if (process.env["NODE_ENV"] === "production") {
    res.setHeader("Strict-Transport-Security", "max-age=31536000; includeSubDomains");
  }
  next();
});

// Dashboard is built separately (see package.json's "build" script) into
// artifacts/dashboard/dist/public and served from here so the dashboard and
// API ship as a single deployable service.
const moduleDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(moduleDir, "..", "..", "..");
const dashboardDist = path.resolve(repoRoot, "artifacts/dashboard/dist/public");

const sessionSecret = process.env["SESSION_SECRET"];
if (!sessionSecret) {
  throw new Error("SESSION_SECRET environment variable is required");
}

app.use(
  session({
    secret: sessionSecret,
    resave: false,
    saveUninitialized: false,
    cookie: {
      httpOnly: true,
      secure: process.env["NODE_ENV"] === "production",
      maxAge: 7 * 24 * 60 * 60 * 1000, // 7 days
      sameSite: "lax",
    },
  }),
);

app.use(
  pinoHttp({
    logger,
    serializers: {
      req(req) {
        return {
          id: req.id,
          method: req.method,
          url: req.url?.split("?")[0],
        };
      },
      res(res) {
        return {
          statusCode: res.statusCode,
        };
      },
    },
  }),
);
// The dashboard is served from this same origin, so cross-origin access is
// off by default. Extra origins can be allowed via CORS_ORIGINS (comma-separated).
const corsOrigins = (process.env["CORS_ORIGINS"] ?? "")
  .split(",")
  .map((o) => o.trim())
  .filter(Boolean);
app.use(cors({ origin: corsOrigins.length ? corsOrigins : false, credentials: true }));
app.use(express.json({ limit: "100kb" }));
app.use(express.urlencoded({ extended: true, limit: "100kb" }));

// Rate limits: login/OAuth endpoints are the most attractive to hammer.
const authRateLimit = rateLimit({
  windowMs: 60 * 1000,
  limit: 20,
  standardHeaders: true,
  legacyHeaders: false,
});
const apiRateLimit = rateLimit({
  windowMs: 60 * 1000,
  limit: 300,
  standardHeaders: true,
  legacyHeaders: false,
});
app.use("/api/auth", authRateLimit);
app.use("/api", apiRateLimit, router);

if (fs.existsSync(dashboardDist)) {
  // Dashboard static/SPA-fallback routes read from disk on every request;
  // rate-limit them against abusive request floods.
  const dashboardRateLimit = rateLimit({
    windowMs: 60 * 1000,
    limit: 300,
    standardHeaders: true,
    legacyHeaders: false,
  });
  app.use(dashboardRateLimit, express.static(dashboardDist));
  app.get(/.*/, dashboardRateLimit, (req, res, next) => {
    if (req.path.startsWith("/api")) {
      next();
      return;
    }
    res.sendFile(path.join(dashboardDist, "index.html"));
  });
} else {
  logger.warn(
    { dashboardDist },
    "Dashboard build output not found; static serving disabled",
  );
}

export default app;
