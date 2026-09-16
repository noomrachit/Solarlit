import path from "node:path";
import express, { type Express } from "express";
import cors from "cors";
import pinoHttp from "pino-http";
import session from "express-session";
import router from "./routes";
import { logger } from "./lib/logger";
const app: Express = express();

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
app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

app.use("/api", router);

// Serve the dashboard's built static files from the same origin/domain as
// the API. Same-origin is required: the dashboard's fetch calls use relative
// "/api/..." paths and the session cookie is scoped to this origin — split
// across two Railway domains, both auth and API calls would break.
// Built by `pnpm --filter @workspace/dashboard run build` (see build command
// on this Railway service) into artifacts/dashboard/dist/public.
const dashboardDistPath = path.join(
  __dirname,
  "..",
  "..",
  "dashboard",
  "dist",
  "public",
);
app.use(express.static(dashboardDistPath));
// SPA fallback for any non-/api route (client-side routing via wouter).
app.get(/^\/(?!api\/).*/, (_req, res) => {
  res.sendFile(path.join(dashboardDistPath, "index.html"));
});

export default app;
