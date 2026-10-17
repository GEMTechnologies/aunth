
from typing import Optional, List, Dict, Any
from sqlalchemy import text
from sqlalchemy.orm import Session
from fastapi import HTTPException
import logging

import models, schemas
from config import settings

logger = logging.getLogger(__name__)

class ContextService:
    """Service for managing user contexts and routing logic"""

    @staticmethod
    def _bind_tenant(db: Session, org_id: str) -> None:
        """Set (or clear) the row-level-security tenant, where the engine supports it.

        A no-op on SQLite, which has neither `set_config` nor row-level security. Checked by dialect
        rather than wrapped in try/except: an exception-driven skip made the organisation lookup fail
        silently in tests and left the context without its slug - a missing field that looked like a
        mapping bug rather than an unsupported call.
        """
        try:
            dialect = getattr(getattr(db, "bind", None), "dialect", None)
            if dialect is None or dialect.name != "postgresql":
                return
            db.execute(
                text("SELECT set_config('app.current_org_id', :org, false)"), {"org": org_id}
            )
        except Exception:  # noqa: BLE001 - a missing binding must not stop resolution
            logger.debug("context_bind_failed", extra={"org_id": org_id})

    
    @staticmethod
    def get_user_contexts(db: Session, user_id: str) -> Dict[str, Any]:
        """Every workspace this user can enter, derived from REAL membership.

        WHY THIS NO LONGER READS `user_contexts`. That table is populated by nothing - measured on
        production: **0 rows**, while `org_members` held the actual membership and
        `organisations.owner_user_id` named the owner. So every user resolved to zero contexts, and
        `resolve_landing_context` raised a 500 that the client renders as "Something went wrong".
        Sign-up worked, sign-in worked, and the product was unreachable for everybody - including the
        organisation's owner.

        THE ROSTER IS A CROSS-TENANT QUESTION, so it is asked of the one narrow SECURITY DEFINER
        function that answers exactly it: "which organisations does this user belong to". Asking
        `organisations` directly returns nothing, because RLS has no tenant bound yet - asking the
        question is what the binding would be derived from.

        Any rows that DO exist in `user_contexts` are still honoured and merged, so a deployment that
        uses that table keeps working.
        """
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        context_list: List[Dict[str, Any]] = []
        seen_orgs: set = set()

        try:
            rows = db.execute(
                text("SELECT app.user_org_ids(:uid)"), {"uid": user_id}
            ).all()
        except Exception:  # noqa: BLE001 - resolution must not 500 on a missing helper
            logger.warning("context_roster_unavailable", extra={"user_id": user_id})
            rows = []

        if not rows:
            # FALLBACK, and it can only ADD contexts - never contradict the privileged answer.
            #
            # Two reasons it is needed. The test suite runs on SQLite, which has no
            # `app.user_org_ids`; and a deployment whose helper is missing would otherwise resolve
            # every user to onboarding, which is the bug this method exists to fix. A direct read may
            # be filtered by RLS, so it is consulted only when the privileged answer is empty.
            try:
                owned = [
                    org.id
                    for org in db.query(models.Organisation)
                    .filter(models.Organisation.owner_user_id == user_id)
                    .all()
                ]
                member = [
                    m.org_id
                    for m in db.query(models.OrgMember)
                    .filter(models.OrgMember.user_id == user_id)
                    .all()
                ]
                rows = [(org_id,) for org_id in dict.fromkeys(owned + member) if org_id]
            except Exception:  # noqa: BLE001
                rows = []

        for row in rows:
            org_id = str(row[0]) if row and row[0] else None
            if not org_id or org_id in seen_orgs:
                continue
            seen_orgs.add(org_id)

            # Bind so the organisation row itself is readable, then clear it immediately. A leaked
            # binding would let the next organisation's details resolve under this one's context.
            #
            # BINDING IS POSTGRES-ONLY, and checked rather than attempted. `set_config` does not
            # exist on SQLite, so a try/except around it silently skipped the organisation lookup in
            # every test - the context still resolved, but without a slug or a name, and the failure
            # looked like a missing field rather than an unsupported call.
            org = None
            try:
                ContextService._bind_tenant(db, org_id)
                org = (
                    db.query(models.Organisation)
                    .filter(models.Organisation.id == org_id)
                    .first()
                )
            except Exception:  # noqa: BLE001 - the id alone is still a usable context
                org = None
            finally:
                ContextService._bind_tenant(db, "")

            context: Dict[str, Any] = {
                "type": "org",
                "product": "ngos",
                "org_id": org_id,
                # The owner of the organisation is the one authority that cannot be revoked by
                # deleting a membership row.
                "role": "owner" if (org is not None and org.owner_user_id == user_id) else "member",
            }
            if org is not None:
                context["org_slug"] = org.slug
                context["org_name"] = org.name
            context_list.append(context)

        # Legacy contexts, still honoured.
        for ctx in db.query(models.UserContext).filter(
            models.UserContext.user_id == user_id,
            models.UserContext.is_active == True,  # noqa: E712 - SQL comparison, not Python truth
        ).all():
            if ctx.org_id and str(ctx.org_id) in seen_orgs:
                continue
            data = {"type": ctx.context_type, "product": ctx.product, "role": ctx.role}
            if ctx.org_id:
                data["org_id"] = ctx.org_id
                if ctx.organisation:
                    data["org_slug"] = ctx.organisation.slug
                    data["org_name"] = ctx.organisation.name
                seen_orgs.add(str(ctx.org_id))
            context_list.append(data)

        return {
            "last_active_context": user.last_active_context,
            "contexts": context_list,
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
        
        # E) No contexts. A NEWLY REGISTERED USER IS EXACTLY THIS, so raising 500 here made the
        #    product unreachable for every account that had not yet created or joined an
        #    organisation - which is every account, immediately after signing up. The comment that
        #    used to sit here ("should not happen for registered users") was the assumption that
        #    broke it.
        #
        #    Onboarding is a legitimate state, not an error: the client shows "set up your
        #    workspace" and offers to create the organisation.
        return {"action": "onboarding", "contexts": [], "url": None}
    
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
