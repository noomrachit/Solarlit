import app from "./app";
import { logger } from "./lib/logger";
import { ensureBillingSchema } from "./db/billingPool";

const rawPort = process.env["PORT"];

if (!rawPort) {
  throw new Error(
    "PORT environment variable is required but was not provided.",
  );
}

const port = Number(rawPort);

if (Number.isNaN(port) || port <= 0) {
  throw new Error(`Invalid PORT value: "${rawPort}"`);
}

ensureBillingSchema().catch((err) => {
  // Non-fatal: billing routes just 503 until the DB is reachable/configured.
  logger.error({ err }, "Failed to ensure billing schema");
});

app.listen(port, (err) => {
  if (err) {
    logger.error({ err }, "Error listening on port");
    process.exit(1);
  }

  logger.info({ port }, "Server listening");
});
