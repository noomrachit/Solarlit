import { Router, type IRouter } from "express";
import healthRouter from "./health";
import authRouter from "./auth";
import moonlitRouter from "./moonlit";
import billingRouter from "./billing";
import { requireAuth } from "../middlewares/requireAuth";

const router: IRouter = Router();

// Public routes
router.use(healthRouter);
router.use(authRouter);
// billing.ts mixes public (config, webhook) and protected (status, subscribe)
// routes internally via userOwnsGuild() checks — mount it before requireAuth
// so the webhook (called by Omise, no session) isn't blocked.
router.use(billingRouter);

// Protected routes — require Discord login
router.use(requireAuth);
router.use(moonlitRouter);

export default router;
