import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App';

function expectLinkButton(name: string, href: string) {
  const link = screen.getByRole('link', { name });
  expect(link).toHaveAttribute('href', href);
  expect(link).toHaveAttribute('target', '_blank');
  expect(link).toHaveAttribute('rel', 'noopener noreferrer');
}

function expectTidesquareButton() {
  expectLinkButton('Mở Tidesquare', 'https://tidesquare.vn');
  expectLinkButton('Mở Tidesquare Global', 'https://tidesquare.com');
}

afterEach(() => {
  delete window.ZAFClient;
  window.history.replaceState({}, '', '/');
});

describe('App outside Zendesk', () => {
  it('renders the fallback card and the button without errors', () => {
    const errorSpy = vi.spyOn(console, 'error');
    render(<App />);
    expect(screen.getByText('Đang chạy ngoài Zendesk')).toBeInTheDocument();
    expectTidesquareButton();
    expect(errorSpy).not.toHaveBeenCalled();
  });
});

describe('App inside Zendesk', () => {
  it('renders ticket info and Slack origin, and invokes resize', async () => {
    window.history.replaceState({}, '', '/?origin=https://acme.zendesk.com&app_guid=abc');
    const handlers: Record<string, () => void> = {};
    const client = {
      get: vi.fn().mockResolvedValue({
        'ticket.id': 42,
        'ticket.subject': 'Help me',
        'ticket.externalId': 'slack:T01:C02:1672531199.000200',
        'ticket.status': 'open'
      }),
      invoke: vi.fn(),
      on: vi.fn((event: string, handler: () => void) => { handlers[event] = handler; })
    };
    window.ZAFClient = { init: () => client };

    render(<App />);
    handlers['app.registered']();

    expect(await screen.findByText('Ticket #42 - Help me')).toBeInTheDocument();
    expect(screen.getByText('C02')).toBeInTheDocument();
    expect(screen.getByText(':eyes:')).toBeInTheDocument();
    expectTidesquareButton();
    await waitFor(() => expect(client.invoke).toHaveBeenCalledWith('resize', expect.objectContaining({ width: '100%' })));
  });
});
