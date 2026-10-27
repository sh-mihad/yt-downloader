import { useRef, useState } from "react"

type MediaFormat = {
  id: string
  kind: "video" | "audio"
  ext: string
  quality: string
  label: string
  premium: boolean
  size: number | null
}

type MediaInfo = {
  url: string
  platform: string
  title: string
  thumbnail: string
  duration: number
  uploader: string
  formats: MediaFormat[]
}

type Progress = {
  status: "queued" | "downloading" | "converting" | "completed" | "failed"
  percent: number
  eta: number | null
  speed_str: string
  downloaded_str: string
  total_str: string
  stage: string
  filename: string | null
  error: string | null
  download_url: string | null
}

async function readError(res: Response): Promise<string> {
  try {
    const body = await res.json()
    return typeof body.detail === "string" ? body.detail : "Something went wrong."
  } catch {
    return "Something went wrong."
  }
}

export default function App() {
  const [url, setUrl] = useState("")
  const [info, setInfo] = useState<MediaInfo | null>(null)
  const [formatId, setFormatId] = useState("")
  const [fetching, setFetching] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [progress, setProgress] = useState<Progress | null>(null)
  const sourceRef = useRef<EventSource | null>(null)

  async function fetchInfo() {
    setError(null)
    setInfo(null)
    setProgress(null)
    setFetching(true)
    try {
      const res = await fetch("/api/media/info", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ url }),
      })
      if (!res.ok) throw new Error(await readError(res))
      const body: MediaInfo = await res.json()
      setInfo(body)
      setFormatId(body.formats[0]?.id ?? "")
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong.")
    } finally {
      setFetching(false)
    }
  }

  async function startDownload() {
    setError(null)
    setProgress(null)
    try {
      const res = await fetch("/api/media/job", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ url, format: formatId }),
      })
      if (!res.ok) throw new Error(await readError(res))
      const body: { job_id: string } = await res.json()

      sourceRef.current?.close()
      const source = new EventSource(`/api/media/progress/${body.job_id}`)
      sourceRef.current = source
      source.onmessage = (ev) => {
        const payload: Progress = JSON.parse(ev.data)
        setProgress(payload)
        if (payload.status === "completed" || payload.status === "failed") {
          source.close()
          if (payload.status === "completed" && payload.download_url) {
            window.location.href = payload.download_url
          }
        }
      }
      source.onerror = () => {
        source.close()
        setError("Lost the progress connection. Try again.")
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong.")
    }
  }

  return (
    <main className="wrap">
      <h1>YouTube downloader — bot-check test</h1>
      <p className="hint">
        Paste a YouTube link, fetch info, pick a quality, download. This exists to check whether YouTube
        extraction works at all from wherever this is hosted — not a product.
      </p>

      <div className="row">
        <input
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://www.youtube.com/watch?v=..."
        />
        <button onClick={fetchInfo} disabled={!url || fetching}>
          {fetching ? "Fetching…" : "Fetch"}
        </button>
      </div>

      {error && <p className="error">{error}</p>}

      {info && (
        <section className="info">
          {info.thumbnail && <img src={info.thumbnail} alt="" />}
          <div className="info-body">
            <h2>{info.title}</h2>
            <p className="muted">{info.uploader}</p>
            <div className="row">
              <select value={formatId} onChange={(e) => setFormatId(e.target.value)}>
                {info.formats.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.kind === "video" ? f.quality : `${f.quality} MP3`} — {f.label}
                  </option>
                ))}
              </select>
              <button onClick={startDownload}>Download</button>
            </div>
          </div>
        </section>
      )}

      {progress && (
        <section className="progress">
          <div className="bar">
            <div className="fill" style={{ width: `${Math.min(100, progress.percent)}%` }} />
          </div>
          <p className="muted">
            {progress.stage} — {progress.percent.toFixed(0)}%
            {progress.status === "downloading" && ` — ${progress.speed_str} — ${progress.downloaded_str} / ${progress.total_str}`}
          </p>
          {progress.status === "failed" && <p className="error">{progress.error}</p>}
        </section>
      )}
    </main>
  )
}
