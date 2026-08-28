from datetime import datetime, timedelta
from typing import Optional, Dict
import secrets
import hashlib
import base64
from authlib.integrations.starlette_client import OAuth
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from fastapi import HTTPException, status
import httpx
from itsdangerous import URLSafeTimedSerializer
import jwt
from dotenv import load_dotenv
load_dotenv()
import os

CLIENT_ID=os.getenv("CLIENT_ID")
CLIENT_SECRET=os.getenv("CLIENT_SECRET")
REDIRECT_URI=os.getenv("REDIRECT_URI")
ADFS_AUTHORIZATION_ENDPOINT=os.getenv("ADFS_AUTHORIZATION_ENDPOINT")
ADFS_TOKEN_ENDPOINT=os.getenv("ADFS_TOKEN_ENDPOINT")
ADFS_USERINFO_ENDPOINT=os.getenv("ADFS_USERINFO_ENDPOINT")
ADFS_ISSUER_URI=os.getenv("ADFS_ISSUER_URI")
SESSION_SECRET=os.getenv("SESSION_SECRET")


class SSOManager:
    def __init__(self):
        self.client_id = CLIENT_ID
        self.client_secret = CLIENT_SECRET
        self.redirect_uri = REDIRECT_URI
        self.authorization_endpoint = ADFS_AUTHORIZATION_ENDPOINT
        self.token_endpoint = ADFS_TOKEN_ENDPOINT
        self.userinfo_endpoint = ADFS_USERINFO_ENDPOINT
        self.issuer = ADFS_ISSUER_URI
       
        # For state management
        self.serializer = URLSafeTimedSerializer(SESSION_SECRET)
       
        # Store for PKCE verifiers (in production, use Redis or database)
        self.pkce_store: Dict[str, str] = {}
   
    def generate_pkce_pair(self) -> tuple[str, str]:
        """Generate PKCE code verifier and challenge"""
        code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('utf-8').rstrip('=')
        code_challenge = create_s256_code_challenge(code_verifier)
        return code_verifier, code_challenge
   
    def generate_state(self) -> str:
        """Generate secure state parameter"""
        return secrets.token_urlsafe(32)
   
    def build_authorization_url(self) -> Dict[str, str]:
        """Build the authorization URL for SSO login"""
        state = self.generate_state()
        code_verifier, code_challenge = self.generate_pkce_pair()
       
        # Store code_verifier with state as key
        self.pkce_store[state] = code_verifier
       
        # Build authorization URL
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": "openid profile email",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256"
        }
       
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        auth_url = f"{self.authorization_endpoint}?{query_string}"
       
        return {
            "authorization_url": auth_url,
            "state": state
        }
   
    async def exchange_code_for_token(self, code: str, state: str) -> Dict:
        """Exchange authorization code for access token"""
        # Retrieve code_verifier
        code_verifier = self.pkce_store.get(state)
        if not code_verifier:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid state parameter or session expired"
            )
       
        # Prepare token request
        token_data = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": code_verifier
        }
       
        async with httpx.AsyncClient(verify=False) as client:
            try:
                response = await client.post(
                    self.token_endpoint,
                    data=token_data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"}
                )
                response.raise_for_status()
                token_response = response.json()
            except httpx.HTTPError as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Failed to exchange code for token: {str(e)}"
                )
       
        # Clean up used code_verifier
        del self.pkce_store[state]
       
        return token_response
       
    def decode_id_token(self, id_token: str) -> Dict:
            """
            Decode ID token without verification (ADFS tokens are already verified by the server)
            In production, you should verify the signature using JWKS
            """
            try:
                decoded = jwt.decode(
                    id_token,
                    options={"verify_signature": False}
                )
                return decoded
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Failed to decode ID token: {str(e)}"
                )

    async def get_user_info(self, access_token: str) -> Dict:
        """Get user information from ADFS using access token"""
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    self.userinfo_endpoint,
                    headers={"Authorization": f"Bearer {access_token}"}
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPError as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Failed to get user info: {str(e)}"
                )

# Global SSO manager instance
sso_manager = SSOManager()
