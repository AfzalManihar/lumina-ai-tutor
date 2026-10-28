"""Razorpay recurring subscription setup and signed webhook activation."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from auth.route import authenticate
from config.db import subscriptions_collection, processed_webhook_events

router=APIRouter(prefix="/billing",tags=["billing"])
class SubscribeRequest(BaseModel):
    plan: Literal["pro"] = "pro"
class BillingProviderError(RuntimeError): pass

def verify_webhook_signature(raw: bytes, supplied: str, secret: str) -> bool:
    expected=hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()
    return bool(supplied) and hmac.compare_digest(expected,supplied)

def _request_subscription(user: dict, plan_id: str) -> dict:
    key=os.getenv("RAZORPAY_KEY_ID","").strip(); secret=os.getenv("RAZORPAY_KEY_SECRET","").strip()
    if not key or not secret: raise RuntimeError("Razorpay credentials are not configured")
    body=json.dumps({"plan_id":plan_id,"total_count":12,"quantity":1,"customer_notify":1,
        "notes":{"user_id":user["user_id"],"username":user["username"]}}).encode()
    auth=base64.b64encode(f"{key}:{secret}".encode()).decode()
    req=urllib.request.Request("https://api.razorpay.com/v1/subscriptions",data=body,
        headers={"Authorization":"Basic "+auth,"Content-Type":"application/json"},method="POST")
    try:
        with urllib.request.urlopen(req,timeout=15) as response: result=json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise BillingProviderError("Razorpay could not create a subscription") from exc
    except Exception as exc:
        raise BillingProviderError("Razorpay is temporarily unavailable") from exc
    if not result.get("id") or not result.get("short_url"):
        raise BillingProviderError("Razorpay returned an incomplete subscription")
    return result

@router.get("/status")
def subscription_status(user=Depends(authenticate)):
    if user["role"]!="Student": raise HTTPException(403,"Student account required")
    sub=subscriptions_collection.find_one({"user_id":user["user_id"]},{"_id":0,"user_id":0})
    return {"subscription":sub or {"status":"free","plan":"free"}}

@router.post("/subscribe")
async def create_subscription(request:SubscribeRequest,user=Depends(authenticate)):
    if user["role"]!="Student": raise HTTPException(403,"Student account required")
    plan_id=os.getenv("RAZORPAY_PLAN_ID_PRO","").strip()
    if not plan_id: raise HTTPException(503,"Configure RAZORPAY_PLAN_ID_PRO from your Razorpay dashboard")
    existing=subscriptions_collection.find_one({"user_id":user["user_id"],"status":{"$in":["created","authenticated","active"]}})
    if existing and existing.get("status")=="active": raise HTTPException(409,"An active subscription already exists")
    if existing and existing.get("status")=="created" and existing.get("checkout_url"):
        return {"subscription_id":existing.get("subscription_id"),"checkout_url":existing["checkout_url"],"status":"created"}
    try: result=await asyncio.wait_for(asyncio.to_thread(_request_subscription,user,plan_id),timeout=18)
    except RuntimeError as exc: raise HTTPException(503,str(exc)) from exc
    except BillingProviderError as exc: raise HTTPException(502,str(exc)) from exc
    except asyncio.TimeoutError as exc: raise HTTPException(504,"Razorpay subscription request timed out") from exc
    now=datetime.now(timezone.utc)
    subscriptions_collection.update_one({"user_id":user["user_id"]},{"$set":{
        "user_id":user["user_id"],"plan":"pro","plan_id":plan_id,"subscription_id":result["id"],
        "status":"created","checkout_url":result["short_url"],"updated_at":now}},upsert=True)
    return {"subscription_id":result["id"],"checkout_url":result["short_url"],"status":"created"}

@router.post("/webhook")
async def razorpay_webhook(request:Request):
    secret=os.getenv("RAZORPAY_WEBHOOK_SECRET","").strip()
    if not secret: raise HTTPException(503,"Razorpay webhook secret is not configured")
    raw=await request.body(); supplied=request.headers.get("X-Razorpay-Signature","")
    if not verify_webhook_signature(raw, supplied, secret): raise HTTPException(400,"Invalid webhook signature")
    try: payload=json.loads(raw); event=payload["event"]; entity=payload["payload"]["subscription"]["entity"]
    except Exception as exc: raise HTTPException(400,"Malformed subscription webhook") from exc
    event_id=request.headers.get("X-Razorpay-Event-Id") or payload.get("id")
    if event_id:
        try: processed_webhook_events.insert_one({"_id":event_id,"received_at":datetime.now(timezone.utc)})
        except Exception as exc:
            if exc.__class__.__name__ == "DuplicateKeyError": return {"received":True,"duplicate":True}
            raise HTTPException(503,"Webhook idempotency storage is unavailable") from exc
    user_id=(entity.get("notes") or {}).get("user_id")
    subscription_id=entity.get("id")
    if not user_id or not subscription_id: raise HTTPException(400,"Webhook lacks subscription ownership metadata")
    states={"subscription.activated":"active","subscription.charged":"active",
            "subscription.cancelled":"cancelled","subscription.halted":"halted",
            "subscription.paused":"paused","subscription.resumed":"active"}
    status=states.get(event)
    if status:
        subscriptions_collection.update_one({"user_id":str(user_id),"subscription_id":subscription_id},{"$set":{
            "status":status,"plan":"pro","plan_id":entity.get("plan_id"),"subscription_id":subscription_id,
            "current_start":entity.get("current_start"),"current_end":entity.get("current_end"),
            "updated_at":datetime.now(timezone.utc)}},upsert=True)
    return {"received":True,"event":event,"processed":bool(status)}
