import React, { useCallback, useEffect, useState } from 'react';
import {
  AgentResult,
  MailSummary,
  Mailbox,
  MailboxList,
  MessagePage,
  connectMailbox,
  fetchMailboxes,
  fetchMailMessages,
} from '../lib/agent';

/**
 * The mailbox screen: what arrived, in which mailbox, and what needs a person.
 *
 * THE FOUR STATES ARE THE WHOLE POINT, exactly as on the agent screen. A request that failed and a
 * mailbox that is genuinely empty are different facts, and rendering both as "no messages" tells an
 * operator their inbox is quiet when the truth is that nobody looked. Every panel therefore
 * distinguishes loading, failed, empty and data, and the failure names the reason.
 *
 * VERIFICATION CODES ARE NEVER DISPLAYED. That is not politeness: a one-time code is a credential
 * for the length of its window, and whoever reads it can complete - or hijack - the registration it
 * belongs to. The server redacts the value before it leaves, and this screen goes further by saying
 * so explicitly, because a masked preview that looks like a bug would invite somebody to "fix" it.
 * What a person actually needs to know is that a code arrived and which mailbox it went to.
 */

type Panel<T> =
  | { kind: 'loading' }
  | { kind: 'failed'; error: string }
  | { kind: 'ready'; value: T };

function panelOf<T>(result: AgentResult<T> | null): Panel<T> {
  if (result === null) return { kind: 'loading' };
  if (!result.ok) return { kind: 'failed', error: result.error };
  return { kind: 'ready', value: result.data };
}

/** A short, unambiguous time. "2 hours ago" beats a raw ISO string for deciding what is recent. */
function when(value?: string | null): string {
  if (!value) return '—';
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return '—';
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

const MailPage: React.FC<{ organisationName?: string }> = () => {
  const [mailboxes, setMailboxes] = useState<Panel<MailboxList> | null>(null);
  const [messages, setMessages] = useState<Panel<MessagePage> | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [showOnlyInbound, setShowOnlyInbound] = useState(false);

  // Connecting the organisation's OWN mailbox. Kept here rather than on a separate screen because it
  // creates a mailbox, and this is the screen where mailboxes live.
  const [showConnect, setShowConnect] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);
  const [connected, setConnected] = useState<string | null>(null);
  const [form, setForm] = useState({
    address: '',
    imap_host: '',
    imap_port: '993',
    imap_user: '',
    imap_password: '',
    use_ssl: true,
  });

  const loadMailboxes = useCallback(async () => {
    setMailboxes(null);
    setMailboxes(panelOf(await fetchMailboxes()));
  }, []);

  const loadMessages = useCallback(
    async (mailboxId: string | null) => {
      setMessages(null);
      const result = await fetchMailMessages({
        mailbox_id: mailboxId ?? undefined,
        direction: showOnlyInbound ? 'INBOUND' : undefined,
        limit: 50,
      });
      setMessages(panelOf(result));
    },
    [showOnlyInbound],
  );

  useEffect(() => {
    loadMailboxes();
  }, [loadMailboxes]);

  useEffect(() => {
    loadMessages(selected);
  }, [selected, loadMessages]);

  const boxes = mailboxes?.kind === 'ready' ? mailboxes.value.mailboxes : [];
  const chosen = boxes.find((box) => box.id === selected) ?? null;

  const submitConnect = async (event: React.FormEvent) => {
    event.preventDefault();
    setConnecting(true);
    setConnectError(null);
    setConnected(null);

    const result = await connectMailbox({
      address: form.address.trim(),
      imap_host: form.imap_host.trim(),
      imap_port: Number(form.imap_port) || 993,
      imap_user: form.imap_user.trim(),
      imap_password: form.imap_password,
      use_ssl: form.use_ssl,
    });
    setConnecting(false);

    if (!result.ok) {
      setConnectError(result.error);
      return;
    }
    setConnected(result.data.address);
    // The password is never read back and is no longer needed in the browser.
    setForm({ address: '', imap_host: '', imap_port: '993', imap_user: '', imap_password: '', use_ssl: true });
    setShowConnect(false);
    loadMailboxes();
  };

  return (
    <div className="space-y-6" data-testid="mail-page">
      <header className="flex items-start justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-900">Mail</h1>
          <p className="text-sm text-gray-600 mt-1">
            Each portal account has its own mailbox, so one portal's verification mail never mixes
            with another's.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 text-sm text-gray-600">
            <input
              type="checkbox"
              checked={showOnlyInbound}
              onChange={(event) => setShowOnlyInbound(event.target.checked)}
              data-testid="filter-inbound"
            />
            Inbound only
          </label>
          <button
            onClick={() => {
              loadMailboxes();
              loadMessages(selected);
            }}
            className="px-3 py-1.5 text-sm font-medium rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50"
            data-testid="refresh"
          >
            Refresh
          </button>
        </div>
      </header>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* ---------------------------------------------------------------- */}
        {/* Mailboxes                                                        */}
        {/* ---------------------------------------------------------------- */}
        <section
          className="bg-white rounded-lg shadow-sm border border-gray-200 p-5"
          data-testid="mailbox-panel"
        >
          <h2 className="text-base font-semibold text-gray-900 mb-3">Mailboxes</h2>

          {mailboxes?.kind === 'loading' && (
            <p className="text-sm text-gray-400" data-testid="mailboxes-loading">
              Loading…
            </p>
          )}

          {mailboxes?.kind === 'failed' && (
            <div className="text-sm" data-testid="mailboxes-failed">
              <p className="text-red-700 font-medium">The mailboxes could not be loaded.</p>
              {/* The reason, not just the failure: a generic error sends nobody anywhere. */}
              <p className="text-gray-600 mt-1">{mailboxes.error}</p>
              <button
                onClick={loadMailboxes}
                className="mt-2 text-blue-700 hover:underline"
                data-testid="mailboxes-retry"
              >
                Try again
              </button>
            </div>
          )}

          {mailboxes?.kind === 'ready' && boxes.length === 0 && (
            <p className="text-sm text-gray-500" data-testid="mailboxes-empty">
              This organisation has no mailbox yet. One is created automatically the first time it
              registers on a portal, so that each portal's mail has somewhere of its own to arrive.
            </p>
          )}

          {mailboxes?.kind === 'ready' && boxes.length > 0 && (
            <>
              <ul className="space-y-2">                <li>
                  <button
                    onClick={() => setSelected(null)}
                    className={`w-full text-left px-3 py-2 rounded-md text-sm ${
                      selected === null
                        ? 'bg-blue-50 border border-blue-200 text-blue-900'
                        : 'border border-transparent hover:bg-gray-50 text-gray-700'
                    }`}
                    data-testid="mailbox-all"
                  >
                    All mail
                  </button>
                </li>
                {boxes.map((box: Mailbox) => (
                  <li key={box.id}>
                    <button
                      onClick={() => setSelected(box.id)}
                      className={`w-full text-left px-3 py-2 rounded-md text-sm ${
                        selected === box.id
                          ? 'bg-blue-50 border border-blue-200 text-blue-900'
                          : 'border border-transparent hover:bg-gray-50 text-gray-700'
                      }`}
                      data-testid={`mailbox-${box.address}`}
                    >
                      <span className="block font-medium break-all">{box.address}</span>
                      <span className="block text-xs text-gray-500 mt-0.5">{box.purpose}</span>
                      <span className="block text-xs text-gray-400 mt-0.5">
                        {box.message_count} message{box.message_count === 1 ? '' : 's'}
                        {box.last_message_at ? ` · last ${when(box.last_message_at)}` : ''}
                        {box.is_primary ? ' · public address' : ''}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>

              {mailboxes.value.unassigned_messages > 0 && (
                <p
                  className="mt-3 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded p-2"
                  data-testid="unassigned-notice"
                >
                  {mailboxes.value.unassigned_messages} message
                  {mailboxes.value.unassigned_messages === 1 ? '' : 's'} arrived before a mailbox was
                  recorded, so {mailboxes.value.unassigned_messages === 1 ? 'it is' : 'they are'} shown
                  under "All mail" only.
                </p>
              )}
            </>
          )}

          {/* ------------------------------------------------------------ */}
          {/* Connect the organisation's OWN mailbox                        */}
          {/*                                                              */}
          {/* Offered on this screen because it creates a mailbox, and it  */}
          {/* is the one route to a verification code that does not depend  */}
          {/* on Cloudflare Email Routing.                                  */}
          {/* ------------------------------------------------------------ */}
          <div className="mt-4 pt-4 border-t border-gray-100">
            {connected && (
              <p className="text-xs text-emerald-700 mb-2" data-testid="connect-ok">
                Connected {connected}. Granada reads it on the sync sweep.
              </p>
            )}

            {!showConnect && (
              <button
                onClick={() => setShowConnect(true)}
                className="text-sm text-blue-700 hover:underline"
                data-testid="show-connect"
              >
                Connect an existing mailbox (e.g. your Gmail)
              </button>
            )}

            {showConnect && (
              <form onSubmit={submitConnect} className="space-y-2" data-testid="connect-form">
                <p className="text-xs text-gray-500">
                  Use an <strong>app password</strong>, not your account password. It is stored
                  encrypted and never shown again.
                </p>
                <input
                  value={form.address}
                  onChange={(e) => setForm({ ...form, address: e.target.value })}
                  placeholder="Mailbox address (e.g. ngo@gmail.com)"
                  className="w-full px-3 py-2 text-sm border border-gray-300 rounded-md"
                  data-testid="connect-address"
                  required
                />
                <input
                  value={form.imap_host}
                  onChange={(e) => setForm({ ...form, imap_host: e.target.value })}
                  placeholder="IMAP host (e.g. imap.gmail.com)"
                  className="w-full px-3 py-2 text-sm border border-gray-300 rounded-md"
                  data-testid="connect-host"
                  required
                />
                <div className="flex gap-2">
                  <input
                    value={form.imap_user}
                    onChange={(e) => setForm({ ...form, imap_user: e.target.value })}
                    placeholder="IMAP username"
                    className="flex-1 px-3 py-2 text-sm border border-gray-300 rounded-md"
                    data-testid="connect-user"
                    required
                  />
                  <input
                    value={form.imap_port}
                    onChange={(e) => setForm({ ...form, imap_port: e.target.value })}
                    placeholder="993"
                    className="w-20 px-3 py-2 text-sm border border-gray-300 rounded-md"
                    data-testid="connect-port"
                    required
                  />
                </div>
                <input
                  type="password"
                  value={form.imap_password}
                  onChange={(e) => setForm({ ...form, imap_password: e.target.value })}
                  placeholder="App password"
                  className="w-full px-3 py-2 text-sm border border-gray-300 rounded-md"
                  data-testid="connect-password"
                  required
                />
                <label className="flex items-center gap-2 text-xs text-gray-600">
                  <input
                    type="checkbox"
                    checked={form.use_ssl}
                    onChange={(e) => setForm({ ...form, use_ssl: e.target.checked })}
                  />
                  Use SSL
                </label>

                {connectError && (
                  <p className="text-xs text-red-700" data-testid="connect-error">
                    {connectError}
                  </p>
                )}

                <div className="flex gap-2">
                  <button
                    type="submit"
                    disabled={connecting}
                    className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50"
                    data-testid="connect-submit"
                  >
                    {connecting ? 'Connecting…' : 'Connect'}
                  </button>
                  <button
                    type="button"
                    onClick={() => setShowConnect(false)}
                    className="px-3 py-1.5 text-sm border border-gray-300 rounded-md text-gray-700"
                  >
                    Cancel
                  </button>
                </div>
              </form>
            )}
          </div>
        </section>

        {/* ---------------------------------------------------------------- */}
        {/* Messages                                                         */}
        {/* ---------------------------------------------------------------- */}
        <section
          className="lg:col-span-2 bg-white rounded-lg shadow-sm border border-gray-200 p-5"
          data-testid="messages-panel"
        >
          <h2 className="text-base font-semibold text-gray-900 mb-1">
            {chosen ? chosen.address : 'All mail'}
          </h2>
          {chosen && <p className="text-xs text-gray-500 mb-3">{chosen.purpose}</p>}

          {messages?.kind === 'loading' && (
            <p className="text-sm text-gray-400" data-testid="messages-loading">
              Loading…
            </p>
          )}

          {messages?.kind === 'failed' && (
            <div className="text-sm" data-testid="messages-failed">
              <p className="text-red-700 font-medium">Mail could not be loaded.</p>
              <p className="text-gray-600 mt-1">{messages.error}</p>
              <button
                onClick={() => loadMessages(selected)}
                className="mt-2 text-blue-700 hover:underline"
                data-testid="messages-retry"
              >
                Try again
              </button>
            </div>
          )}

          {messages?.kind === 'ready' && messages.value.messages.length === 0 && (
            <p className="text-sm text-gray-500" data-testid="messages-empty">
              Nothing has arrived in {chosen ? 'this mailbox' : 'any mailbox'} yet.
            </p>
          )}

          {messages?.kind === 'ready' && messages.value.messages.length > 0 && (
            <ul className="divide-y divide-gray-100">
              {messages.value.messages.map((message: MailSummary) => (
                <li key={message.id} className="py-3" data-testid={`message-${message.id}`}>
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-gray-900 truncate">
                        {message.subject || '(no subject)'}
                      </p>
                      <p className="text-xs text-gray-500 mt-0.5 break-all">
                        {message.direction === 'INBOUND' ? 'from' : 'to'}{' '}
                        {message.sender_name || message.sender || 'unknown'}
                        {message.mailbox_address ? ` → ${message.mailbox_address}` : ''}
                      </p>
                    </div>
                    <div className="text-right shrink-0">
                      <span
                        className={`inline-block text-[11px] px-2 py-0.5 rounded-full ${
                          message.direction === 'INBOUND'
                            ? 'bg-blue-50 text-blue-700'
                            : 'bg-gray-100 text-gray-600'
                        }`}
                      >
                        {message.direction}
                      </span>
                      <p className="text-xs text-gray-400 mt-1">{when(message.created_at)}</p>
                    </div>
                  </div>

                  {message.preview && (
                    <p className="text-xs text-gray-600 mt-1 line-clamp-2">{message.preview}</p>
                  )}

                  {/* Said out loud, so a masked preview is never mistaken for a rendering bug. */}
                  {message.code_redacted && (
                    <p
                      className="mt-1 text-[11px] text-emerald-700 bg-emerald-50 border border-emerald-200 rounded px-2 py-1 inline-block"
                      data-testid="code-redacted"
                    >
                      A verification code arrived. Its value is withheld here on purpose — it is a
                      one-time credential, and the agent uses it without showing it.
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}

          {messages?.kind === 'ready' && messages.value.has_more && (
            <p className="mt-3 text-xs text-gray-400" data-testid="more-available">
              Showing the most recent {messages.value.count}. Older mail is available through the API
              cursor.
            </p>
          )}
        </section>
      </div>
    </div>
  );
};

export default MailPage;
