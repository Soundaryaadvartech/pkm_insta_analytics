import os
from datetime import datetime, timezone
import traceback
import asyncio
from asyncio import Semaphore
from aiohttp import ClientSession, ClientConnectorError
from dotenv import load_dotenv
from sqlalchemy import func
from fastapi import HTTPException, status
from database.models import PostInsights, Posts

load_dotenv()

BASE_URL = os.getenv("BASE_URL")

shared_session = None

#To prevent socket exhaustion in http methods
async def startup_event(): 
    global shared_session
    shared_session = ClientSession()

async def shutdown_event():
    global shared_session
    if shared_session:
        await shared_session.close()

async def fetch_post_metrics(post_id, token):
    global shared_session
    async with shared_session.get(f"{BASE_URL}{post_id}?fields=like_count&access_token={token}") as response:
        likes_data = await response.json()

    async with shared_session.get(f"{BASE_URL}{post_id}/insights?metric=reach,saved&access_token={token}") as response:
        insights_data = await response.json()

    return likes_data, insights_data


async def process_posts_async(posts, token, concurrency=50, retries=3, delay=2):
    semaphore = Semaphore(concurrency)  # Limit to 50 concurrent tasks
    tasks = []

    async def safe_fetch(post_id, attempt=1):
        async with semaphore:
            try:
                return await fetch_post_metrics(post_id, token)
            except ClientConnectorError as e:
                # Retry logic for transient errors
                if attempt <= retries:
                    print(f"Retrying post {post_id} (Attempt {attempt}/{retries}) due to: {e}")
                    await asyncio.sleep(delay * attempt)  # Exponential backoff
                    return await safe_fetch(post_id, attempt + 1)
                else:
                    print(f"Failed to fetch post {post_id} after {retries} attempts: {e}")
                    raise e
            except Exception as e:
                # Handle other exceptions and log them
                print(f"Error fetching metrics for post {post_id}: {e}")
                raise e

    for post in posts:
        post_id = post["id"]
        tasks.append(safe_fetch(post_id))

    # Gather all tasks and let exceptions propagate if retries fail
    results = await asyncio.gather(*tasks)
    return results


async def get_posts_async(url, params):
    global shared_session
    try:
        async with shared_session.get(url, params=params) as response:
            if response.status != 200:
                raise HTTPException(
                    status_code=response.status,
                    detail=f"Failed to fetch posts: {await response.text()}",
                )
            return await response.json()
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while fetching posts: {str(e)}",
        )

def store_posts_and_metrics(posts, metrics, db):
    """
    Store posts and their metrics in the database.
    """
    try:
        # Create or update posts in bulk
        for i, post in enumerate(posts):
            post_id = post["id"]
            media_type = post["media_type"]
            media_url = post.get("media_url", None)
            raw_timestamp = post.get("timestamp")

            if not media_url:
                print(f"Post {post_id} is missing 'media_url'. Skipping...")

            # Parse post creation timestamp
            post_created = None
            if raw_timestamp:
                utc_time = datetime.strptime(raw_timestamp, "%Y-%m-%dT%H:%M:%S%z")
                post_created = utc_time.strftime("%Y-%m-%d")

            # Check if the post already exists in the database
            existing_post = db.query(Posts).filter(Posts.post_id == post_id).first()
            if not existing_post:
                db_post = Posts(
                    post_id=post_id,
                    media_type=media_type,
                    media_url=media_url,
                    post_created=post_created,
                    created_ts=datetime.now(timezone.utc),
                    updated_ts=datetime.now(timezone.utc),
                )
                db.add(db_post)
                db.commit()
                db.refresh(db_post)
            else:
                db_post = existing_post

            # Process metrics
            likes_data, insights_data = metrics[i]
            like_count = likes_data.get("like_count", 0)
            reach = next(
                (item["values"][0]["value"] for item in insights_data.get("data", []) if item["name"] == "reach"), 0
            )
            saves = next(
                (item["values"][0]["value"] for item in insights_data.get("data", []) if item["name"] == "saved"), 0
            )

            # Fetch existing metrics and calculate differences
            existing_sums = db.query(
                func.sum(PostInsights.likes).label("total_likes"),
                func.sum(PostInsights.saves).label("total_saves"),
                func.sum(PostInsights.reach).label("total_reach"),
            ).filter(PostInsights.posts_id == db_post.id).first()

            total_likes = existing_sums.total_likes or 0
            total_saves = existing_sums.total_saves or 0
            total_reach = existing_sums.total_reach or 0

            new_likes = like_count - total_likes
            new_saves = saves - total_saves
            new_reach = reach - total_reach

            today_date = datetime.now(timezone.utc).date()
            existing_insight = db.query(PostInsights).filter(
                PostInsights.posts_id == db_post.id,
                func.date(PostInsights.created_ts) == today_date,
            ).first()

            if existing_insight:
                existing_insight.reach += new_reach
                existing_insight.likes += new_likes
                existing_insight.saves += new_saves
                existing_insight.updated_ts = datetime.now(timezone.utc)
                db.commit()
                db.refresh(existing_insight)
            else:
                db_insight = PostInsights(
                    posts_id=db_post.id,
                    reach=new_reach,
                    likes=new_likes,
                    saves=new_saves,
                    created_ts=datetime.now(timezone.utc),
                    updated_ts=datetime.now(timezone.utc),
                )
                db.add(db_insight)
                db.commit()
                db.refresh(db_insight)

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to store posts and metrics: {str(e)}",
        )