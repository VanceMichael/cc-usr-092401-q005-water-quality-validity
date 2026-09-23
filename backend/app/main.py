import math

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from .database import engine, Base
from .migrate import run_lightweight_migrations
from .routers import ponds, batches, stocking, feeding, water_quality, medication, costs, harvest, analysis

Base.metadata.create_all(bind=engine)
run_lightweight_migrations(engine)

app = FastAPI(
    title="水产养殖管理系统",
    description="一个完整的水产养殖管理系统，支持塘口管理、投苗记录、日常管理、成本核算、出塘销售和养殖周期分析",
    version="1.0.0"
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """把 pydantic / 请求体错误统一为 {errors:[{field,message}]}。

    前端据此在表单中精确定位错误字段；同时剔除 inf/nan 等无法 JSON
    序列化的原始输入（默认处理器会因此抛出 500）。
    """
    errors = []
    for err in exc.errors():
        loc = [p for p in err.get("loc", ()) if p not in ("body", "query", "path")]
        field = str(loc[-1]) if loc else "form"
        message = err.get("msg", "字段值不合法")
        if message.startswith("Value error, "):
            message = message[len("Value error, "):]
        ctx = err.get("ctx") or {}
        inner = ctx.get("error")
        if inner is not None and str(inner) and str(inner) != message:
            message = str(inner)
        # 只保留可安全序列化的信息
        if isinstance(message, float) and not math.isfinite(message):
            message = "必须是有限数值"
        errors.append({"field": field, "message": str(message)})
    return JSONResponse(status_code=422, content={"errors": errors})

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ponds.router)
app.include_router(batches.router)
app.include_router(stocking.router)
app.include_router(feeding.router)
app.include_router(water_quality.router)
app.include_router(medication.router)
app.include_router(costs.router)
app.include_router(harvest.router)
app.include_router(analysis.router)

@app.get("/")
def root():
    return {
        "message": "欢迎使用水产养殖管理系统API",
        "docs": "/docs",
        "version": "1.0.0"
    }

@app.get("/health")
def health_check():
    return {"status": "healthy"}
