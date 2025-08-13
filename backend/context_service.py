
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from fastapi import HTTPException
import logging

import models, schemas
from config import settings

logger = logging.getLogger(__name__)

class ContextService:
    """Service for managing user contexts and routing logic"""
    
    @staticmethod
    def get_user_contexts(db: Session, user_id: str) -> Dict[str, Any]:
        """Get all available contexts for a user"""
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Get all active contexts
        contexts = db.query(models.UserContext).filter(
            models.UserContext.user_id == user_id,
            models.UserContext.is_active == True
        ).all()
        
        # Build context list
        context_list = []
        for ctx in contexts:
            context_data = {
                "type": ctx.context_type,
                "product": ctx.product,
                "role": ctx.role
            }
            
            if ctx.org_id:
                context_data["org_id"] = ctx.org_id
                if ctx.organisation:
                    context_data["org_slug"] = ctx.organisation.slug
                    context_data["org_name"] = ctx.organisation.name
            
            context_list.append(context_data)
        
        return {
            "last_active_context": user.last_active_context,
            "contexts": context_list
        }
    
    @staticmethod
    def set_last_active_context(db: Session, user_id: str, context: Dict[str, Any]) -> None:
        """Set user's last active context"""
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Validate that user has access to this context
        if not ContextService._validate_user_context(db, user_id, context):
            raise HTTPException(status_code=403, detail="User does not have access to this context")
        
        user.last_active_context = context
        db.commit()
    
    @staticmethod
    def _validate_user_context(db: Session, user_id: str, context: Dict[str, Any]) -> bool:
        """Validate that user has access to the specified context"""
        query = db.query(models.UserContext).filter(
            models.UserContext.user_id == user_id,
            models.UserContext.context_type == context.get("type"),
            models.UserContext.is_active == True
        )
        
        if context.get("org_id"):
            query = query.filter(models.UserContext.org_id == context["org_id"])
        
        return query.first() is not None
    
    @staticmethod
    def resolve_landing_context(
        db: Session, 
        user_id: str, 
        host: Optional[str] = None,
        redirect_uri: Optional[str] = None
    ) -> Dict[str, Any]:
        """Resolve where user should land based on context resolution algorithm"""
        
        meta = ContextService.get_user_contexts(db, user_id)
        
        # A) Honor explicit destination
        if redirect_uri and ContextService._validate_redirect_uri(redirect_uri, meta):
            target_context = ContextService._context_for_url(redirect_uri, meta)
            if target_context:
                return {"context": target_context, "url": redirect_uri, "action": "redirect"}
        
        # Subdomain hint
        product = ContextService._subdomain_to_product(host)
        if product:
            matches = ContextService._contexts_for_product(meta["contexts"], product)
            if len(matches) == 1:
                return {
                    "context": matches[0], 
                    "url": ContextService._url_for_context(matches[0]),
                    "action": "redirect"
                }
            elif len(matches) > 1:
                return {"action": "show_product_picker", "product": product, "contexts": matches}
        
        # B) Use last active context
        if meta["last_active_context"] and ContextService._context_still_valid(db, user_id, meta["last_active_context"]):
            return {
                "context": meta["last_active_context"],
                "url": ContextService._url_for_context(meta["last_active_context"]),
                "action": "redirect"
            }
        
        # C) Single available context
        if len(meta["contexts"]) == 1:
            return {
                "context": meta["contexts"][0],
                "url": ContextService._url_for_context(meta["contexts"][0]),
                "action": "redirect"
            }
        
        # D) Multiple contexts - show picker
        if len(meta["contexts"]) > 1:
            return {"action": "show_picker", "contexts": meta["contexts"]}
        
        # E) No contexts - should not happen for registered users
        raise HTTPException(status_code=500, detail="User has no available contexts")
    
    @staticmethod
    def _subdomain_to_product(host: Optional[str]) -> Optional[str]:
        """Extract product from subdomain"""
        if not host:
            return None
        
        subdomain_map = {
            "academia": "student",
            "ngos": "ngos", 
            "business": "business",
            "jobs": "jobs"
        }
        
        for subdomain, product in subdomain_map.items():
            if host.startswith(f"{subdomain}."):
                return product
        
        return None
    
    @staticmethod
    def _contexts_for_product(contexts: List[Dict], product: str) -> List[Dict]:
        """Filter contexts by product"""
        if product == "student":
            return [ctx for ctx in contexts if ctx["type"] == "student"]
        else:
            return [ctx for ctx in contexts if ctx.get("product") == product]
    
    @staticmethod
    def _url_for_context(context: Dict[str, Any]) -> str:
        """Generate URL for a context"""
        base_url = settings.frontend_url
        
        if context["type"] == "student":
            return f"{base_url}/student/dashboard"
        elif context["type"] == "org":
            product = context.get("product", "ngos")
            org_slug = context.get("org_slug", context.get("org_id"))
            return f"{base_url}/{product}/{org_slug}/dashboard"
        
        return f"{base_url}/dashboard"
    
    @staticmethod
    def _validate_redirect_uri(uri: str, meta: Dict) -> bool:
        """Validate redirect URI against allowlist"""
        # Simple validation - in production you'd have a proper allowlist
        allowed_patterns = [
            settings.frontend_url,
            "https://academia.granada.tld",
            "https://ngos.granada.tld", 
            "https://business.granada.tld"
        ]
        
        return any(uri.startswith(pattern) for pattern in allowed_patterns)
    
    @staticmethod
    def _context_for_url(url: str, meta: Dict) -> Optional[Dict]:
        """Extract context from URL"""
        # Simple URL parsing - you'd implement proper routing logic
        if "/student/" in url:
            return next((ctx for ctx in meta["contexts"] if ctx["type"] == "student"), None)
        elif "/ngos/" in url:
            # Extract org slug and find matching context
            # This is simplified - you'd parse the URL properly
            return next((ctx for ctx in meta["contexts"] if ctx.get("product") == "ngos"), None)
        
        return None
    
    @staticmethod
    def _context_still_valid(db: Session, user_id: str, context: Dict) -> bool:
        """Check if stored context is still valid"""
        return ContextService._validate_user_context(db, user_id, context)

    @staticmethod
    def create_student_context(db: Session, user_id: str) -> models.UserContext:
        """Create student context for user"""
        context = models.UserContext(
            user_id=user_id,
            context_type="student",
            product="academia",
            role="student"
        )
        db.add(context)
        
        # Set as last active context
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if user:
            user.last_active_context = {"type": "student"}
        
        db.commit()
        return context
    
    @staticmethod
    def create_org_context(db: Session, user_id: str, org_id: str, product: str, role: str) -> models.UserContext:
        """Create organization context for user"""
        context = models.UserContext(
            user_id=user_id,
            context_type="org",
            org_id=org_id,
            product=product,
            role=role
        )
        db.add(context)
        
        # Set as last active context
        user = db.query(models.User).filter(models.User.id == user_id).first()
        org = db.query(models.Organisation).filter(models.Organisation.id == org_id).first()
        if user and org:
            user.last_active_context = {
                "type": "org",
                "org_id": org_id,
                "product": product,
                "role": role
            }
        
        db.commit()
        return context
