# satopt/middleware.py
from django.shortcuts import redirect

CANONICAL_HOST = "satisfactory-solver.com"

class HttpsRedirectMiddleware:
    """
    Forces HTTPS even if the platform doesn't set X-Forwarded-Proto as expected.
    Runs before other middleware.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        scheme = request.headers.get("X-Forwarded-Proto") or request.scheme
        if scheme != "https":
            return redirect(
                f"https://{request.get_host()}{request.get_full_path()}",
                permanent=True
            )
        return self.get_response(request)


class CanonicalDomainMiddleware:
    """
    Redirects www.satisfactory-solver.com → satisfactory-solver.com (HTTPS).
    Runs after HTTPS is enforced so the redirect target is always https.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = request.get_host().split(":")[0].lower()
        if host == f"www.{CANONICAL_HOST}":
            return redirect(
                f"https://{CANONICAL_HOST}{request.get_full_path()}",
                permanent=True
            )
        return self.get_response(request)


class SecurityHeadersMiddleware:
    """
    Adds a safety net to auto-upgrade any http:// subresources (band-aid for mixed content).
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["Content-Security-Policy"] = "upgrade-insecure-requests"
        response["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response["X-Content-Type-Options"] = "nosniff"
        return response
