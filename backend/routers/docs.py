from fastapi import APIRouter

router = APIRouter()

@router.get("/")
async def docs_test():
    return {"message": "Docs router is active!"}