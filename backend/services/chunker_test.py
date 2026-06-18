"""
Run this directly to test chunking on any Python file:
  python -m backend.services.chunker_test
"""
from backend.services.chunker import CodeChunker

SAMPLE_PYTHON = '''
import os
from pathlib import Path

DATABASE_URL = "sqlite:///test.db"

class UserService:
    """Handles user operations."""

    def __init__(self, db):
        self.db = db

    def get_user(self, user_id: int):
        """Fetch a user by ID."""
        return self.db.query(User).filter(User.id == user_id).first()

    def create_user(self, email: str, password: str):
        """Create a new user with hashed password."""
        hashed = hash_password(password)
        user = User(email=email, password_hash=hashed)
        self.db.add(user)
        self.db.commit()
        return user

def hash_password(password: str) -> str:
    import hashlib
    return hashlib.sha256(password.encode()).hexdigest()
'''

SAMPLE_JS = '''
import React, { useState } from 'react';

const API_URL = 'http://localhost:8000';

export const fetchUser = async (userId) => {
    const response = await fetch(`${API_URL}/users/${userId}`);
    return response.json();
};

export default function UserCard({ userId }) {
    const [user, setUser] = useState(null);

    useEffect(() => {
        fetchUser(userId).then(setUser);
    }, [userId]);

    return <div>{user?.name}</div>;
}
'''

def main():
    chunker = CodeChunker()

    print("=" * 60)
    print("PYTHON FILE — AST CHUNKING")
    print("=" * 60)
    py_chunks = chunker.chunk_file(SAMPLE_PYTHON, "services/user_service.py", "Python")
    for chunk in py_chunks:
        print(f"\n[Chunk {chunk.chunk_index}] {chunk.node_type}: {chunk.node_name}")
        print(f"  Lines {chunk.start_line}–{chunk.end_line}")
        print(f"  Length: {len(chunk.content)} chars")
        print(f"  Preview: {chunk.content[:80].replace(chr(10), ' ')}...")

    print("\n" + "=" * 60)
    print("JAVASCRIPT FILE — REGEX CHUNKING")
    print("=" * 60)
    js_chunks = chunker.chunk_file(SAMPLE_JS, "components/UserCard.jsx", "JavaScript")
    for chunk in js_chunks:
        print(f"\n[Chunk {chunk.chunk_index}] {chunk.node_type}: {chunk.node_name}")
        print(f"  Lines {chunk.start_line}–{chunk.end_line}")
        print(f"  Preview: {chunk.content[:80].replace(chr(10), ' ')}...")

    print("\n" + "=" * 60)
    print(f"Python: {len(py_chunks)} chunks | JS: {len(js_chunks)} chunks")

if __name__ == "__main__":
    main()