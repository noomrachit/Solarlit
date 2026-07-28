import { Router, type IRouter } from "express";
import healthRouter from "./health";
import moonlitRouter from "./moonlit";

const router: IRouter = Router();

router.use(healthRouter);
router.use(moonlitRouter);

export default router;
