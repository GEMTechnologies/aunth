from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .database import engine, Base
from .router import router

app = FastAPI(title="Granada Auth Service", version="1.0.0")

# create tables
Base.metadata.create_all(bind=engine)

# CORS (adjust for production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

@app.get("/healthz")
def healthz():
    return {"status": "ok"}
