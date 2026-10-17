from fastapi import APIRouter, Body, Header, Query

from .workspace_chat import owner_hash


def workspace_chat_routes(service):
    router = APIRouter(prefix='/api/workspace-chat')

    @router.get('')
    def status(x_idi_chat_key: str = Header(default='')):
        return service.status(x_idi_chat_key)

    @router.get('/memory')
    def memories(thread_id: str | None = None, project_id: str | None = None, case_id: str | None = None,
                 query: str = Query(default='', max_length=160), x_idi_chat_key: str = Header(default='')):
        return service.memory.list(owner_hash(x_idi_chat_key), thread_id=thread_id, project_id=project_id, case_id=case_id, query=query)

    @router.get('/memory/export')
    def export_memories(thread_id: str | None = None, project_id: str | None = None, case_id: str | None = None,
                        x_idi_chat_key: str = Header(default='')):
        return {'filename': 'idi-ai-memory.md', 'markdown': service.memory.export(owner_hash(x_idi_chat_key),
            thread_id=thread_id, project_id=project_id, case_id=case_id)}

    @router.get('/memory/summaries/{scope_key}')
    def read_summary(scope_key: str, thread_id: str | None = None, project_id: str | None = None,
                     case_id: str | None = None, x_idi_chat_key: str = Header(default='')):
        return service.memory.managed_summary(owner_hash(x_idi_chat_key), scope_key,
            thread_id=thread_id, project_id=project_id, case_id=case_id)

    @router.patch('/memory/summaries/{scope_key}')
    def edit_summary(scope_key: str, data: dict = Body(...), thread_id: str | None = None,
                     project_id: str | None = None, case_id: str | None = None, x_idi_chat_key: str = Header(default='')):
        return service.memory.managed_summary(owner_hash(x_idi_chat_key), scope_key, data,
            thread_id=thread_id, project_id=project_id, case_id=case_id)

    @router.post('/memory')
    def create_memory(data: dict = Body(...), thread_id: str | None = None, project_id: str | None = None,
                      case_id: str | None = None, x_idi_chat_key: str = Header(default='')):
        return service.memory.create(owner_hash(x_idi_chat_key), data, thread_id=thread_id, project_id=project_id, case_id=case_id)

    @router.patch('/memory/{memory_id}')
    def update_memory(memory_id: str, data: dict = Body(...), thread_id: str | None = None,
                      project_id: str | None = None, case_id: str | None = None, x_idi_chat_key: str = Header(default='')):
        return service.memory.update(owner_hash(x_idi_chat_key), memory_id, data, thread_id=thread_id, project_id=project_id, case_id=case_id)

    @router.patch('/threads/{thread_id}/summary')
    def update_summary(thread_id: str, data: dict = Body(...), x_idi_chat_key: str = Header(default='')):
        return service.memory.edit_summary(owner_hash(x_idi_chat_key), thread_id, data)

    @router.get('/threads/{thread_id}')
    def thread(thread_id: str, x_idi_chat_key: str = Header(default='')):
        return service.get(x_idi_chat_key, thread_id)

    @router.post('/turns')
    def send(data: dict = Body(...), x_idi_chat_key: str = Header(default='')):
        return service.send(x_idi_chat_key, data)

    @router.post('/turns/{turn_id}/cancel')
    def cancel(turn_id: str, x_idi_chat_key: str = Header(default='')):
        return service.cancel(x_idi_chat_key, turn_id)

    return router
