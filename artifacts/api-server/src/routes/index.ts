import { Router, type IRouter } from "express";
import healthRouter from "./health";
import authRouter from "./auth";
import moonlitRouter from "./moonlit";
import { requireAuth } from "../middlewares/requireAuth";

const router: IRouter = Router();

// Public routes
router.use(healthRouter);
router.use(authRouter);

// Protected routes — require Discord login
router.use(requireAuth);
router.use(moonlitRouter);

export default router;
