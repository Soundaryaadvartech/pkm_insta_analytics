import os
import traceback
from datetime import datetime, timezone
import requests
from sqlalchemy import func
from sqlalchemy.orm import Session
from dotenv import load_dotenv, set_key
from fastapi import APIRouter,HTTPException, status, Depends
from fastapi.responses import JSONResponse
from database.models import SocialMedia, EngagedAudienceAge, EngagedAudienceGender, EngagedAudienceLocation, PostInsights,Posts
from utilities.access_token import refresh_access_token, is_access_token_expired, generate_new_long_lived_token
from database.database import get_db
from utilities.fetch_posts_helper import process_posts_async, store_posts_and_metrics, get_posts_async

router = APIRouter()

load_dotenv()

DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")
DB_HOST = os.getenv("DB_HOST")
BASE_URL = os.getenv("BASE_URL")
BLT_ACCESS_TOKEN = os.getenv("BLT_ACCESS_TOKEN")
BLT_INSTAGRAM_ACCOUNT_ID = os.getenv("BLT_INSTAGRAM_ACCOUNT_ID")
APP_ID = os.getenv("META_APP_ID")
APP_SECRET = os.getenv("META_APP_SECRET")
LONG_LIVED_TOKEN = os.getenv("LONG_LIVED_TOKEN")

@router.get("/fetch_insights_blt")
def fetch_insights_blt(db: Session = Depends(get_db)):
    """
    Fetch a summarized version of Instagram insights, showing only important metrics.
    Automatically refreshes access token if needed.
    """
    try:
        global BLT_ACCESS_TOKEN

        # Refresh the short-lived token
        if is_access_token_expired(BLT_ACCESS_TOKEN):
            try:
                refreshed_token = refresh_access_token(APP_ID, APP_SECRET, LONG_LIVED_TOKEN)
                set_key('.env', 'BLT_ACCESS_TOKEN', refreshed_token)
                load_dotenv()
                BLT_ACCESS_TOKEN = os.getenv("BLT_ACCESS_TOKEN")
            except Exception as e:
                try:
                    new_long_lived_token = generate_new_long_lived_token()
                    set_key('.env', 'LONG_LIVED_TOKEN', new_long_lived_token)
                    load_dotenv()
                    new_blt_access_token = refresh_access_token(APP_ID, APP_SECRET, new_long_lived_token)
                    set_key('.env', 'BLT_ACCESS_TOKEN', new_blt_access_token)
                    load_dotenv()
                    BLT_ACCESS_TOKEN = os.getenv("BLT_ACCESS_TOKEN")
                except Exception as gen_error:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Failed to generate new long-lived token: {str(gen_error)}"
                    )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to refresh access token: {str(e)}"
                )

        # Fetch Instagram account details
        account_url = f"{BASE_URL}{BLT_INSTAGRAM_ACCOUNT_ID}?fields=id,username,followers_count&access_token={BLT_ACCESS_TOKEN}"
        account_response = requests.get(account_url, timeout=120)

        if account_response.status_code != 200:
            raise HTTPException(
                status_code=account_response.status_code,
                detail=f"Failed to fetch account details: {account_response.text}"
            )
        account_data = account_response.json()

        # Fetch insights
        insights_url = f"{BASE_URL}{BLT_INSTAGRAM_ACCOUNT_ID}/insights?metric=reach,accounts_engaged,website_clicks&period=day&metric_type=total_value&access_token={BLT_ACCESS_TOKEN}"
        insights_response = requests.get(insights_url, timeout=120)

        if insights_response.status_code != 200:
            raise HTTPException(
                status_code=insights_response.status_code,
                detail=f"Failed to fetch insights: {insights_response.text}"
            )
        insights_data = insights_response.json()

        # Extract insights
        reach, accounts_engaged, website_clicks = None, None, None
        for item in insights_data.get("data", []):
            if item.get("name") == "reach" and "total_value" in item:
                reach = item["total_value"].get("value")
            if item.get("name") == "accounts_engaged" and "total_value" in item:
                accounts_engaged = item["total_value"].get("value")
            if item.get("name") == "website_clicks" and "total_value" in item:
                website_clicks = item["total_value"].get("value")

        # Combine results
        result = {
            "username": account_data.get("username"),
            "followers_count": account_data.get("followers_count"),
            "reach": reach,
            "accounts_engaged": accounts_engaged,
            "website_clicks": website_clicks,
        }

        # Calculate the sum of existing records
        existing_sums = db.query(
            func.sum(SocialMedia.followers).label("total_followers"),
            func.sum(SocialMedia.reach).label("total_reach"),
            func.sum(SocialMedia.accounts_engaged).label("total_accounts_engaged"),
            func.sum(SocialMedia.website_clicks).label("total_website_clicks"),
        ).first()

        # Extract values or default to 0
        total_followers = existing_sums.total_followers or 0
        total_reach = existing_sums.total_reach or 0
        total_accounts_engaged = existing_sums.total_accounts_engaged or 0
        total_website_clicks = existing_sums.total_website_clicks or 0

        # Calculate the differences (new data - sum of existing records)
        new_followers = result["followers_count"] - total_followers
        new_reach = result["reach"] - total_reach
        new_accounts_engaged = result["accounts_engaged"] - total_accounts_engaged
        new_website_clicks = result["website_clicks"] - total_website_clicks

        # Get today's date in UTC
        today_date = datetime.now(timezone.utc).date()

        # Check if a record for today already exists
        existing_record = db.query(SocialMedia).filter(func.date(SocialMedia.created_ts) == today_date).first()

        if existing_record:
            # Update today's record with calculated differences
            existing_record.followers += new_followers
            existing_record.reach += new_reach
            existing_record.accounts_engaged += new_accounts_engaged
            existing_record.website_clicks += new_website_clicks
            existing_record.updated_ts = datetime.now(timezone.utc)
            db.commit()
            db.refresh(existing_record)
        else:
            # Insert a new record with calculated differences
            socialmedia_analytics = SocialMedia(
                username=result["username"],
                followers=new_followers,
                reach=new_reach,
                accounts_engaged=new_accounts_engaged,
                website_clicks=new_website_clicks,
                created_ts=datetime.now(timezone.utc),
                updated_ts=datetime.now(timezone.utc),
            )
            db.add(socialmedia_analytics)
            db.commit()
            db.refresh(socialmedia_analytics)

        return JSONResponse(content=result)

    except HTTPException as e:
        db.rollback()
        traceback.print_exc()
        return JSONResponse(status_code=e.status_code, content={"error": e.detail})
    except Exception as e:
        db.rollback()
        traceback.print_exc()
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"error": "Something went wrong."})


@router.get("/engaged_audience_demographics")
def engaged_audience_demographics(db: Session = Depends(get_db)):
    try:
        global BLT_ACCESS_TOKEN

        # Refresh the short-lived token
        if is_access_token_expired(BLT_ACCESS_TOKEN):
            try:
                refreshed_token = refresh_access_token(APP_ID, APP_SECRET, LONG_LIVED_TOKEN)
                set_key('.env', 'BLT_ACCESS_TOKEN', refreshed_token)
                load_dotenv()  # Reload the updated .env file
                BLT_ACCESS_TOKEN = os.getenv("BLT_ACCESS_TOKEN")  # Get updated token
            except Exception as e:
                try:
                    new_long_lived_token = generate_new_long_lived_token()
                    set_key('.env', 'LONG_LIVED_TOKEN', new_long_lived_token)
                    load_dotenv()  # Reload the updated .env file
                    BLT_ACCESS_TOKEN = refresh_access_token(APP_ID, APP_SECRET, new_long_lived_token)
                    set_key('.env', 'BLT_ACCESS_TOKEN', BLT_ACCESS_TOKEN)
                    load_dotenv()  # Reload the updated .env file
                except Exception as gen_error:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Failed to generate new long-lived token: {str(gen_error)}"
                    )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to refresh access token: {str(e)}"
                )

        # Define the API URLs
        insights_url = f"{BASE_URL}{BLT_INSTAGRAM_ACCOUNT_ID}/insights"
        params = {
            "metric": "engaged_audience_demographics",
            "period": "lifetime",
            "timeframe": "this_week",
            "metric_type": "total_value",
            "access_token": BLT_ACCESS_TOKEN,
        }

        # Fetch demographic data by breakdown types
        age_response = requests.get(insights_url, params={**params, "breakdown": "age"}, timeout=120)
        gender_response = requests.get(insights_url, params={**params, "breakdown": "gender"}, timeout=120)
        city_response = requests.get(insights_url, params={**params, "breakdown": "city"}, timeout=120)

        if age_response.status_code != 200:
            raise HTTPException(
                status_code=age_response.status_code,
                detail=f"Failed to fetch engaged audience age group: {age_response.text}"
            )

        if gender_response.status_code != 200:
            raise HTTPException(
                status_code=gender_response.status_code,
                detail=f"Failed to fetch engaged audience gender distribution: {gender_response.text}"
            )

        if city_response.status_code != 200:
            raise HTTPException(
                status_code=city_response.status_code,
                detail=f"Failed to fetch engaged audience city distribution: {city_response.text}"
            )

        # Parse the response data
        age_data = age_response.json()
        gender_data = gender_response.json()
        city_data = city_response.json()

        today_date = datetime.now(timezone.utc).date()
        socialmedia_entry = (
            db.query(SocialMedia)
            .filter(func.date(SocialMedia.created_ts) == today_date)
            .order_by(SocialMedia.created_ts.desc())
            .first()
        )
        if not socialmedia_entry:
            raise HTTPException(status_code=404, detail="Social media record not found.")

        socialmedia_id = socialmedia_entry.id

        # Helper function to process and store data
        def process_and_store_data(data, breakdown_type, table_model, attribute_name):
            processed_data = []
            for item in data.get("data", []):
                if item.get("name") == "engaged_audience_demographics" and "total_value" in item:
                    breakdowns = item["total_value"].get("breakdowns", [])
                    for breakdown in breakdowns:
                        if "results" in breakdown:
                            for result in breakdown["results"]:
                                dimension_values = result.get("dimension_values", [])
                                new_count = result.get("value")

                                if dimension_values:
                                    value = dimension_values[0]

                                    # Fetch the sum of all existing counts for this dimension (across all records)
                                    existing_total = db.query(func.sum(table_model.count)).filter(
                                        table_model.socialmedia_id == socialmedia_id,
                                        getattr(table_model, attribute_name) == value
                                    ).scalar() or 0


                                    # Calculate the difference: new_count - existing_total
                                    count_difference = new_count - existing_total

                                    # Fetch the existing entry for the current day
                                    existing_entry = db.query(table_model).filter(
                                        table_model.socialmedia_id == socialmedia_id,
                                        getattr(table_model, attribute_name) == value,
                                        func.date(table_model.created_ts) == func.current_date()
                                    ).first()

                                    if existing_entry:
                                        existing_entry.count += count_difference
                                        existing_entry.updated_ts = datetime.now(timezone.utc)  # Update timestamp
                                        db.commit()
                                        db.refresh(existing_entry)
                                    else:
                                        # Create a new record if none exists
                                        instance = table_model(
                                            socialmedia_id=socialmedia_id,
                                            **{attribute_name: value},
                                            count=count_difference,
                                            created_ts=datetime.now(timezone.utc),
                                            updated_ts=datetime.now(timezone.utc),
                                        )
                                        db.add(instance)

                                    # Append the processed data
                                    processed_data.append({
                                        attribute_name: value,
                                        "count": new_count
                                    })
            db.commit()
            return processed_data

        # Process and store age, gender, and city distributions
        age_group = process_and_store_data(age_data, "age", EngagedAudienceAge, "age_group")
        gender_distribution = process_and_store_data(gender_data, "gender", EngagedAudienceGender, "gender")
        city_distribution = process_and_store_data(city_data, "city", EngagedAudienceLocation, "city")

        # Prepare the final result
        result = {
            "age_group": age_group,
            "gender_distribution": gender_distribution,
            "city_distribution": city_distribution
        }

        return result

    except HTTPException as e:
        db.rollback()
        traceback.print_exc()
        return JSONResponse(status_code=e.status_code, content={"error": e.detail})
    except Exception:
        db.rollback()
        traceback.print_exc()
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"error": "Something went wrong."})

@router.get("/fetch_all_posts")
async def fetch_all_posts(db: Session = Depends(get_db)):
    try:
        global BLT_ACCESS_TOKEN

        # Refresh the short-lived token if expired
        if is_access_token_expired(BLT_ACCESS_TOKEN):
            try:
                refreshed_token = refresh_access_token(APP_ID, APP_SECRET, LONG_LIVED_TOKEN)
                os.environ["BLT_ACCESS_TOKEN"] = refreshed_token
                BLT_ACCESS_TOKEN = refreshed_token
            except Exception:
                new_long_lived_token = generate_new_long_lived_token()
                os.environ["LONG_LIVED_TOKEN"] = new_long_lived_token
                BLT_ACCESS_TOKEN = refresh_access_token(APP_ID, APP_SECRET, new_long_lived_token)
                os.environ["BLT_ACCESS_TOKEN"] = BLT_ACCESS_TOKEN

        # Fetch all posts
        all_posts = []
        posts_url = f"{BASE_URL}{BLT_INSTAGRAM_ACCOUNT_ID}/media"
        params = {
            "fields": "id,media_type,media_url,timestamp",
            "access_token": BLT_ACCESS_TOKEN,
            "limit": 100,
        }

        MAX_PAGES = 100  # Prevent infinite loops
        while posts_url and len(all_posts) < MAX_PAGES * params["limit"]:
            response = await get_posts_async(posts_url, params)
            all_posts.extend(response.get("data", []))
            posts_url = response.get("paging", {}).get("next")

        if not all_posts:
            return JSONResponse(content={"message": "No posts found."})

        # Process posts asynchronously
        metrics = await process_posts_async(all_posts, BLT_ACCESS_TOKEN)

        # Store in database
        store_posts_and_metrics(all_posts, metrics, db)

        return JSONResponse(content={"message": "Successfully fetched all posts and metrics."})

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")
