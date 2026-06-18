from fastapi import APIRouter

router = APIRouter()

@router.get("/")
async def test_chat_endpoint():
    return {"message": "Chat router is working perfectly!"}