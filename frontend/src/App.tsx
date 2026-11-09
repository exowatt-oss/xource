import { useCallback, useEffect, useState, type ChangeEvent, type FormEvent } from 'react'

type Role = 'viewer' | 'editor' | 'manager'
type Space = { id: string; name: string; my_role: Role; is_personal: boolean; visibility: string }
type Version = { number: number; size: number; message?: string; created_at: string; author_label: string }
type Node = { id: string; kind: 'folder' | 'file'; name: string; version?: Version }
type Comment = { id: string; body: string; author_label?: string; created_at: string }
type User = { email: string; name?: string; is_admin: boolean }

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch('/api' + path, { credentials: 'same-origin', ...init })
  if (response.status === 401) throw new Error('Sign in through your configured identity provider to use Xource.')
  if (!response.ok) {
    let detail = response.statusText
    try { detail = (await response.json()).detail ?? detail } catch { /* non-JSON failure */ }
    throw new Error(`${response.status}: ${detail}`)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

const json = (value: object) => ({ headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) })
const size = (bytes: number) => bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KiB`

export default function App() {
  const [user, setUser] = useState<User | null>(null)
  const [spaces, setSpaces] = useState<Space[]>([])
  const [space, setSpace] = useState<Space | null>(null)
  const [folders, setFolders] = useState<{ id: string; name: string }[]>([])
  const [nodes, setNodes] = useState<Node[]>([])
  const [selected, setSelected] = useState<Node | null>(null)
  const [versions, setVersions] = useState<Version[]>([])
  const [comments, setComments] = useState<Comment[]>([])
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const refreshSpaces = useCallback(async () => {
    const result = await api<Space[]>('/spaces')
    setSpaces(result)
    setSpace(current => result.find(s => s.id === current?.id) ?? result[0] ?? null)
  }, [])

  const refreshFolder = useCallback(async () => {
    if (!space) return
    const parent = folders.at(-1)?.id
    const result = await api<{ items: Node[] }>(`/spaces/${space.id}/children${parent ? '?parent_id=' + parent : ''}`)
    setNodes(result.items)
  }, [space, folders])

  const refreshFile = useCallback(async () => {
    if (!selected || selected.kind !== 'file') return
    const [latest, notes] = await Promise.all([
      api<Version[]>(`/nodes/${selected.id}/versions`),
      api<Comment[]>(`/nodes/${selected.id}/comments`),
    ])
    setVersions(latest); setComments(notes)
  }, [selected])

  useEffect(() => {
    api<User>('/auth/me').then(setUser).then(refreshSpaces).catch(e => setError(String(e)))
  }, [refreshSpaces])
  useEffect(() => { refreshFolder().catch(e => setError(String(e))) }, [refreshFolder])
  useEffect(() => { refreshFile().catch(e => setError(String(e))) }, [refreshFile])

  async function run(action: () => Promise<unknown>, refresh = refreshFolder) {
    setError(''); setMessage('')
    try { await action(); await refresh(); setMessage('Saved') }
    catch (e) { setError(String(e)) }
  }

  function createSpace(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const name = new FormData(form).get('name')?.toString().trim()
    if (!name) return
    void run(() => api('/spaces', { method: 'POST', ...json({ name }) }), refreshSpaces)
    form.reset()
  }

  function createFolder(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!space) return
    const form = event.currentTarget
    const name = new FormData(form).get('name')?.toString().trim()
    if (!name) return
    void run(() => api(`/spaces/${space.id}/folders`, { method: 'POST', ...json({ name, parent_id: folders.at(-1)?.id ?? null }) }))
    form.reset()
  }

  function upload(event: ChangeEvent<HTMLInputElement>) {
    if (!space || !event.target.files?.length) return
    const form = new FormData()
    for (const file of event.target.files) form.append('files', file)
    if (folders.length) form.append('parent_id', folders.at(-1)!.id)
    void run(() => api(`/spaces/${space.id}/files`, { method: 'POST', body: form }))
    event.target.value = ''
  }

  function addComment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!selected) return
    const form = event.currentTarget
    const body = new FormData(form).get('body')?.toString().trim()
    if (!body) return
    void run(() => api(`/nodes/${selected.id}/comments`, { method: 'POST', ...json({ body }) }), refreshFile)
    form.reset()
  }

  return (
    <div className="shell">
      <header><img src="/icon.svg" alt="" width="36" height="36"/><h1>Xource</h1><span>Versioned files</span><strong>{user?.name ?? user?.email}</strong></header>
      {error && <p role="alert" className="error">{error}</p>}
      {message && <p role="status" className="success">{message}</p>}
      <div className="layout">
        <aside><h2>Spaces</h2><form onSubmit={createSpace}><label htmlFor="space-name">New space</label><input id="space-name" name="name" required placeholder="Team documents"/><button>Create</button></form>
          <nav aria-label="Spaces">{spaces.map(s => <button key={s.id} className={space?.id === s.id ? 'active' : ''} onClick={() => { setSpace(s); setFolders([]); setSelected(null) }}>{s.name} <small>{s.my_role}</small></button>)}</nav>
        </aside>
        <main><h2>{space?.name ?? 'Choose a space'}</h2>
          {space && <><div className="toolbar"><button onClick={() => { setFolders([]); setSelected(null) }}>Root</button>{folders.map((folder, i) => <button key={folder.id} onClick={() => { setFolders(folders.slice(0, i + 1)); setSelected(null) }}> / {folder.name}</button>)}</div>
            {space.my_role !== 'viewer' && <div className="actions"><form onSubmit={createFolder}><input name="name" aria-label="Folder name" placeholder="New folder" required/><button>Create folder</button></form><label className="upload">Upload file<input type="file" multiple onChange={upload}/></label></div>}
            <ul className="files">{nodes.map(node => <li key={node.id}><button onClick={() => node.kind === 'folder' ? (setFolders([...folders, { id: node.id, name: node.name }]), setSelected(null)) : setSelected(node)}>{node.kind === 'folder' ? '▣' : '▤'} {node.name}</button>{node.version && <small>v{node.version.number} · {size(node.version.size)}</small>}</li>)}</ul>
            {nodes.length === 0 && <p className="empty">This folder is empty. Upload a file to create its first immutable version.</p>}
          </>}
        </main>
        {selected && <section className="details"><h2>{selected.name}</h2><a href={`/api/nodes/${selected.id}/content`} target="_blank" rel="noreferrer">Open / download</a>
          {selected.name.match(/\.(png|jpe?g|gif|webp)$/i) && <iframe title="File preview" sandbox="" src={`/api/nodes/${selected.id}/content?disposition=inline`}/>}
          <h3>Version history</h3><ol>{versions.map(v => <li key={v.number}><a href={`/api/nodes/${selected.id}/content?version=${v.number}`}>v{v.number}</a> · {size(v.size)} · {v.author_label}</li>)}</ol>
          <h3>Comments</h3><ul>{comments.map(c => <li key={c.id}>{c.body}</li>)}</ul>
          <form onSubmit={addComment}><label htmlFor="comment">Add comment</label><textarea id="comment" name="body" required/><button>Post</button></form>
        </section>}
      </div>
    </div>
  )
}
