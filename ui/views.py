import json
import os
from django.http import JsonResponse, HttpResponseBadRequest, HttpResponseForbidden, HttpResponseNotAllowed
from django.shortcuts import render, redirect
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.contrib.auth import get_user_model
from django.contrib.auth import login as auth_login
from django.contrib.auth.forms import UserCreationForm
from django import forms
from .models import DefaultSettings, SavedSettings

User = get_user_model()

# Load data.json once (metadata: items/resources/recipes)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE_DIR, 'data', 'data.json')
with open(DATA_PATH, 'r', encoding='utf-8') as f:
    GAME_DATA = json.load(f)

def index(request):
    # serves the vanilla JS UI (template path: ui/templates/ui/index.html)
    return render(request, 'ui/index.html')

def metadata(request):
    # items/resources/recipes arrays with id + name/display
    items = [{"id": k, "name": v.get("name") or k} for k, v in GAME_DATA.get("items", {}).items()]
    resources = [{"id": k, "name": v.get("name") or k} for k, v in GAME_DATA.get("resources", {}).items()]
    recipes = []
    for k, v in GAME_DATA.get("recipes", {}).items():
        recipes.append({"id": k, "name": v.get("name") or k, "display": v.get("name") or k})
    return JsonResponse({"items": items, "resources": resources, "recipes": recipes})

def default_settings(request):
    obj = DefaultSettings.objects.order_by('-id').first()
    if not obj:
        # Only on the first run, seed from ui/data/default.json
        base_dir = os.path.dirname(os.path.abspath(__file__))
        default_path = os.path.join(base_dir, 'data', 'default.json')
        with open(default_path, 'r', encoding='utf-8') as f:
            payload = json.load(f)

        # Create and save once so future calls use DB
        obj = DefaultSettings.objects.create(
            resource_limits=payload.get("resource_limits", {}),
            weights=payload.get("weights", {}),
            recipes_off=payload.get("recipes_off", []),
            inputs=payload.get("inputs", {}),
            outputs=payload.get("outputs", {}),
            max_item=payload.get("max_item", False),
            checkbox_Nuclear_Waste=payload.get("checkbox_Nuclear Waste", False),
        )

    payload = {
        "resource_limits": obj.resource_limits,
        "weights": obj.weights,
        "recipes_off": obj.recipes_off,
        "inputs": obj.inputs,
        "outputs": obj.outputs,
        "max_item": obj.max_item,
        "checkbox_Nuclear Waste": obj.checkbox_Nuclear_Waste,
    }
    return JsonResponse(payload)

@csrf_exempt
def optimize(request):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required")
    try:
        body = json.loads(request.body.decode('utf-8'))
        settings = body.get("settings") or {}
    except Exception as e:
        return HttpResponseBadRequest(f"Invalid JSON: {e}")

    settings.setdefault("resource_limits", {})
    settings.setdefault("weights", {})
    settings.setdefault("recipes_off", [])
    settings.setdefault("inputs", {})
    settings.setdefault("outputs", {})
    settings.setdefault("max_item", False)
    settings["checkbox_Nuclear Waste"] = bool(settings.get("checkbox_Nuclear Waste", False))

    from .optimizer.main import optimize_production
    result = optimize_production(GAME_DATA, settings)
    return JsonResponse(result, safe=False)

# ---------------- Auth-aware saved settings ----------------

def api_session(request):
    """Tiny endpoint for the front-end to know if a user is logged in."""
    if request.user.is_authenticated:
        return JsonResponse({"authenticated": True, "username": request.user.get_username()})
    return JsonResponse({"authenticated": False})

@csrf_exempt
def saved_settings_collection(request):
    """
    GET  /api/saved-settings/                     -> list current user's presets (or [] if anonymous)
    POST /api/saved-settings/ {name, settings}    -> upsert preset for current user (requires auth)
    """
    if request.method == "GET":
        if not request.user.is_authenticated:
            return JsonResponse([], safe=False)
        rows = SavedSettings.objects.filter(owner=request.user).order_by('name').values('id','name','updated_at')
        out = [{"id": r["id"], "name": r["name"], "updated_at": r["updated_at"].strftime("%Y-%m-%d %H:%M")} for r in rows]
        return JsonResponse(out, safe=False)

    if request.method == "POST":
        if not request.user.is_authenticated:
            return HttpResponseForbidden("Authentication required.")
        try:
            body = json.loads(request.body.decode("utf-8"))
            name = (body.get("name") or "").strip()
            settings = body.get("settings") or {}
            if not name:
                return HttpResponseBadRequest("Missing name")
            obj, _ = SavedSettings.objects.update_or_create(
                owner=request.user, name=name,
                defaults={"settings": settings}
            )
            return JsonResponse({"id": obj.id, "name": obj.name, "updated_at": obj.updated_at.strftime("%Y-%m-%d %H:%M")})
        except Exception as e:
            return HttpResponseBadRequest(str(e))

    return HttpResponseNotAllowed(["GET", "POST"])

def load_settings_view(request):
    """
    GET /api/saved-settings/load?id=...
    Loads only if the current user owns the record.
    """
    sid = request.GET.get("id")
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Authentication required.")
    if not sid:
        return HttpResponseBadRequest("Specify id")
    try:
        obj = SavedSettings.objects.get(id=sid, owner=request.user)
    except SavedSettings.DoesNotExist:
        return JsonResponse({"error":"Not found"}, status=404)
    return JsonResponse(obj.settings)

@csrf_exempt
def delete_saved_setting(request, pk=None):
    """
    DELETE /api/saved-settings/<id>/
    POST   /api/saved-settings/delete?id=<id>   (fallback)
    Only the owner can delete.
    """
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Authentication required.")

    if request.method == "DELETE":
        if pk is None:
            return HttpResponseBadRequest("Missing id in URL")
        try:
            obj = SavedSettings.objects.get(id=pk, owner=request.user)
        except SavedSettings.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)
        obj.delete()
        return JsonResponse({"deleted": pk})

    if request.method == "POST":
        sid = request.GET.get("id")
        if not sid:
            try:
                body = json.loads(request.body or "{}")
                sid = body.get("id")
            except Exception:
                pass
        if not sid:
            return HttpResponseBadRequest("Provide id")

        try:
            obj = SavedSettings.objects.get(id=sid, owner=request.user)
        except SavedSettings.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)
        obj.delete()
        return JsonResponse({"deleted": sid})

    return HttpResponseNotAllowed(["DELETE", "POST"])

class SignupForm(UserCreationForm):
    email = forms.EmailField(required=True)

    class Meta(UserCreationForm.Meta):
        # Use the same user model as UserCreationForm and include email
        fields = ("username", "email")

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        if commit:
            user.save()
        return user

def signup(request):
    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            auth_login(request, user)
            return redirect("/")
        # Useful while debugging: uncomment to see exact errors in runserver console
        # print(form.errors.as_json())
    else:
        form = SignupForm()
    return render(request, "registration/signup.html", {"form": form})

@ensure_csrf_cookie
def session_info(request):
    if request.user.is_authenticated:
        return JsonResponse({"authenticated": True, "username": request.user.username})
    return JsonResponse({"authenticated": False})