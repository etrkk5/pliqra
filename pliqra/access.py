"""Shared authorization predicates used by management, reporting and inference."""
import json

from fastapi import HTTPException


def project_ids(store, principal, management=False):
    if principal["role"] == "admin":
        return [r["id"] for r in store.all("SELECT id FROM projects")]
    if principal["kind"] == "key":
        return [] if management else [principal["key"]["project_id"]]
    sql = "SELECT DISTINCT project_id FROM memberships WHERE user_id=?"
    if management:
        sql += " AND role='manager'"
    direct = {r["project_id"] for r in store.all(sql, (principal["id"],))}
    team_sql = "SELECT tp.project_id FROM team_projects tp JOIN team_members tm ON tm.team_id=tp.team_id WHERE tm.user_id=?"
    if management:
        team_sql += " AND tp.role='manager'"
    return sorted(direct | {r["project_id"] for r in store.all(team_sql, (principal["id"],))})


def require_project(store, principal, project_id, management=False):
    if project_id not in project_ids(store, principal, management):
        raise HTTPException(403, "You do not have access to this project")


def service_allowed(store, principal, port):
    if principal["role"] == "admin":
        return True
    if port.get("project_id") and port["project_id"] not in project_ids(store, principal):
        return False
    if principal["kind"] == "key":
        allowed = json.loads(principal["key"]["allowed_ports"])
        return not allowed or port["id"] in allowed
    return True


def event_scope(store, principal, alias=""):
    prefix = alias + "." if alias else ""
    if principal["role"] == "admin":
        return "1=1", []
    managed = project_ids(store, principal, management=True)
    condition = f"{prefix}principal=?"
    args = [principal["id"]]
    if managed:
        condition += f" OR {prefix}project_id IN ({','.join('?' for _ in managed)})"
        args.extend(managed)
    return "(" + condition + ")", args


def sync_group_memberships(store, provider_id, user_id, names):
    names = set(names)
    source = "idp:" + provider_id
    store.run("DELETE FROM memberships WHERE user_id=? AND source=?", (user_id, source))
    rules = store.all("SELECT * FROM group_rules WHERE provider_id=?", (provider_id,))
    grants = {}
    for rule in rules:
        if rule["group_name"] in names:
            project = rule["project_id"]
            if grants.get(project) != "manager":
                grants[project] = rule["role"]
    for project, role in grants.items():
        store.insert("memberships", {"project_id": project, "user_id": user_id, "role": role, "source": source})
