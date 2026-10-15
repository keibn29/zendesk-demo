import { useCallback, useEffect, useRef, useState } from 'react';
import {
  buildSlackDeepLink,
  buildSlackWebLink,
  formatEpochSecondsUtc,
  getReactionConfig,
  parseSlackExternalId
} from './slack';
import { fetchTicket, useStatusChanged, useZafClient, type TicketData } from './zaf';

type View =
  | { name: 'outside' }
  | { name: 'loading' }
  | { name: 'error'; message: string }
  | { name: 'loaded'; ticket: TicketData };

async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // fall through to the textarea fallback
    }
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'absolute';
  ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.select();
  const ok = document.execCommand('copy');
  document.body.removeChild(ta);
  if (!ok) throw new Error('copy failed');
}

export default function App() {
  const client = useZafClient();
  const containerRef = useRef<HTMLDivElement>(null);
  const [view, setView] = useState<View>(client ? { name: 'loading' } : { name: 'outside' });
  const [status, setStatus] = useState('');
  const [announcement, setAnnouncement] = useState('');
  const [copied, setCopied] = useState(false);

  const load = useCallback(() => {
    if (!client) return;
    setView({ name: 'loading' });
    setAnnouncement('Loading Slack origin details…');
    fetchTicket(client)
      .then((ticket) => {
        setStatus(ticket.status);
        setView({ name: 'loaded', ticket });
        setAnnouncement('Ticket details loaded.');
      })
      .catch((err: unknown) => {
        console.error('Error fetching ticket data:', err);
        const message = err instanceof Error && err.message
          ? err.message
          : 'Failed to retrieve ticket information from Zendesk.';
        setView({ name: 'error', message });
        setAnnouncement('Error loading ticket data.');
      });
  }, [client]);

  // Load once the ZAF app is registered.
  useEffect(() => {
    if (!client) return;
    client.on('app.registered', load);
    return () => client.off?.('app.registered', load);
  }, [client, load]);

  const onStatusChanged = useCallback((next: string) => {
    setStatus(next);
    setAnnouncement('Ticket status changed. Expected reaction updated.');
  }, []);
  useStatusChanged(client, onStatusChanged);

  // Resize the Zendesk iframe whenever the rendered content changes size.
  useEffect(() => {
    const el = containerRef.current;
    if (!client || !el) return;
    const resize = () => {
      const height = Math.max(Math.ceil(el.getBoundingClientRect().height) + 16, 160);
      client.invoke('resize', { width: '100%', height: height + 'px' });
    };
    resize();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(resize);
    observer.observe(el);
    return () => observer.disconnect();
  }, [client]);

  const ticket = view.name === 'loaded' ? view.ticket : null;
  const parsed = ticket ? parseSlackExternalId(ticket.externalId) : null;
  const reaction = getReactionConfig(status);

  const webLink = parsed?.type === 'valid' ? buildSlackWebLink(parsed.channelId, parsed.messageTs) : '';

  const handleCopy = () => {
    if (!webLink) return;
    copyText(webLink)
      .then(() => {
        setCopied(true);
        setAnnouncement('Slack web link copied to clipboard.');
        setTimeout(() => setCopied(false), 2000);
      })
      .catch((err: unknown) => {
        console.error('Failed to copy link:', err);
        setAnnouncement('Failed to copy Slack link.');
      });
  };

  return (
    <div id="app-container" className="app-container" ref={containerRef}>
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">{announcement}</div>

      {ticket && (
        <p className="ticket-info">
          Ticket #{ticket.id}{ticket.subject ? ` - ${ticket.subject}` : ''}
        </p>
      )}

      {view.name === 'outside' && (
        <div className="state-card state-card--muted" role="status">
          <div className="state-icon" aria-hidden="true">ℹ️</div>
          <p className="state-message">Đang chạy ngoài Zendesk</p>
        </div>
      )}

      {view.name === 'loading' && (
        <div className="state-card" role="status" aria-busy="true">
          <div className="spinner" aria-hidden="true" />
          <p className="state-message">Loading Slack origin details…</p>
        </div>
      )}

      {view.name === 'error' && (
        <div className="state-card state-card--error" role="alert">
          <div className="state-icon" aria-hidden="true">⚠️</div>
          <h2 className="state-title">Unable to Load Ticket Data</h2>
          <p className="state-message">{view.message}</p>
          <button type="button" className="btn btn-secondary" onClick={load}>Retry</button>
        </div>
      )}

      {parsed?.type === 'empty' && (
        <div className="state-card state-card--muted">
          <div className="state-icon" aria-hidden="true">ℹ️</div>
          <h2 className="state-title">No Slack Origin</h2>
          <p className="state-message">
            This ticket does not have an external Slack reference. It was not created from a Slack message.
          </p>
        </div>
      )}

      {parsed?.type === 'malformed' && (
        <div className="state-card state-card--warning">
          <div className="state-icon" aria-hidden="true">⚠️</div>
          <h2 className="state-title">Unrecognized External ID</h2>
          <p className="state-message">
            The ticket external ID does not match the expected Slack bridge format
            (<code>slack:{'{team_id}:{channel_id}:{message_ts}'}</code>).
          </p>
          <div className="raw-box">
            <span className="raw-label">Raw External ID:</span>
            <code className="raw-code">{parsed.raw || '(empty)'}</code>
          </div>
        </div>
      )}

      {parsed?.type === 'valid' && (
        <main className="ready-view">
          <section className="section" aria-labelledby="heading-origin">
            <div className="section-header">
              <h2 id="heading-origin" className="section-title">Slack Origin</h2>
            </div>
            <dl className="coords-list">
              <div className="coord-item">
                <dt className="coord-label">Team ID (Workspace)</dt>
                <dd className="coord-value">{parsed.teamId}</dd>
              </div>
              <div className="coord-item">
                <dt className="coord-label">Channel ID</dt>
                <dd className="coord-value">{parsed.channelId}</dd>
              </div>
              <div className="coord-item">
                <dt className="coord-label">Message Timestamp</dt>
                <dd className="coord-value">
                  <span>{parsed.messageTs}</span>
                  <span className="coord-subtext">
                    {formatEpochSecondsUtc(parsed.messageTs) ? `(${formatEpochSecondsUtc(parsed.messageTs)})` : ''}
                  </span>
                </dd>
              </div>
            </dl>

            <div className="actions-group">
              <a
                href={buildSlackDeepLink(parsed.teamId, parsed.channelId, parsed.messageTs)}
                className="btn btn-primary"
                target="_blank"
                rel="noopener noreferrer"
              >
                Open in Slack App
              </a>
              <a href={webLink} className="btn btn-secondary" target="_blank" rel="noopener noreferrer">
                Open in Slack Web
              </a>
              <button
                type="button"
                className={'btn btn-outline' + (copied ? ' btn-copied' : '')}
                aria-label="Copy deterministic Slack web link"
                onClick={handleCopy}
              >
                {copied ? 'Copied!' : 'Copy Slack Link'}
              </button>
            </div>
          </section>

          <section className="section" aria-labelledby="heading-reaction">
            <div className="section-header">
              <h2 id="heading-reaction" className="section-title">Configured Reaction</h2>
              <span className={'badge ' + reaction.badgeClass}>{reaction.badgeLabel}</span>
            </div>
            <div className="reaction-display">
              <span className="reaction-emoji" aria-hidden="true">{reaction.emoji}</span>
              <div className="reaction-meta">
                <div className="reaction-name">:{reaction.name}:</div>
                <div className="reaction-caption">Expected for current ticket status</div>
              </div>
            </div>
            <p className="reaction-disclaimer">
              Configured reaction reflects the emoji mapping expected for this ticket status. This is not real-time delivery confirmation from Slack.
            </p>
          </section>
        </main>
      )}

      <div className="actions-group">
        <a
          href="https://tidesquare.vn"
          target="_blank"
          rel="noopener noreferrer"
          className="button btn btn-primary"
        >
          Mở Tidesquare
        </a>
      </div>
    </div>
  );
}
