from dotenv import load_dotenv
from fastapi import FastAPI
from routers.routers import router
from database.database import Base, engine
from utilities.fetch_posts_helper import startup_event, shutdown_event

load_dotenv()

app = FastAPI(title = "Instagram Insights", on_startup=[startup_event], on_shutdown=[shutdown_event])

app.include_router(router, prefix='/api')
Base.metadata.create_all(bind=engine)