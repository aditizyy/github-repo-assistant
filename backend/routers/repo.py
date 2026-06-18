from fastapi import APIRouter

router = APIRouter()

@router.get("/")
def list_repositories():
    """Phase 2 will fill this in."""
    return {"message": "Repository router — Phase 2"}