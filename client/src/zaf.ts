import { useEffect, useState } from 'react';

/** True when Zendesk loaded the iframe (it appends origin and app_guid to the URL). */
export function isInsideZendesk(search: string = window.location.search): boolean {
  const params = new URLSearchParams(search);
  return Boolean(params.get('origin') && params.get('app_guid'));
}

/** Returns the ZAF client, or null when running outside Zendesk. Initialized once. */
export function useZafClient(): ZafClient | null {
  const [client] = useState<ZafClient | null>(() =>
    isInsideZendesk() && window.ZAFClient ? window.ZAFClient.init() : null
  );
  return client;
}

export interface TicketData {
  id: string;
  subject: string;
  externalId: string | null;
  status: string;
}

const str = (v: unknown): string => (v == null ? '' : String(v));

export async function fetchTicket(client: ZafClient): Promise<TicketData> {
  const data = await client.get(['ticket.id', 'ticket.subject', 'ticket.externalId', 'ticket.status']);
  return {
    id: str(data['ticket.id']),
    subject: str(data['ticket.subject']),
    externalId: data['ticket.externalId'] == null ? null : str(data['ticket.externalId']),
    status: str(data['ticket.status'])
  };
}

/** Subscribes to ticket status changes; returns the new status via callback. */
export function useStatusChanged(client: ZafClient | null, onStatus: (status: string) => void) {
  useEffect(() => {
    if (!client) return;
    const handler = (data?: unknown) => {
      const next = typeof data === 'string' ? data : (data as { newValue?: string } | undefined)?.newValue;
      if (next) {
        onStatus(next);
      } else {
        client.get('ticket.status').then((res) => onStatus(str(res['ticket.status']))).catch(console.error);
      }
    };
    client.on('ticket.status.changed', handler);
    return () => client.off?.('ticket.status.changed', handler);
  }, [client, onStatus]);
}
