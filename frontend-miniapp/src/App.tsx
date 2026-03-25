import { useEffect, useState } from 'react';
import WebApp from '@twa-dev/sdk';
import { RotateCw, Monitor, Search, LayoutTemplate } from 'lucide-react';

declare global {
  interface Window {
    Telegram?: any;
  }
}

// Optional: define your Valentine API endpoint if hosted separately.
// For local dev, you can run the Vite dev server with a proxy or define it explicitly.
const API_URL = import.meta.env.VITE_VALENTINE_API_URL || 'http://localhost:8001';

function App() {
  const [projectId, setProjectId] = useState<string>('');
  const [searchInput, setSearchInput] = useState<string>('');
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [status, setStatus] = useState<'idle' | 'loading' | 'live' | 'offline'>('idle');
  
  // Track forced reloads of the iframe
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    // Notify Telegram that the app is ready
    WebApp.ready();
    WebApp.expand(); // Make it full height
    
    // Check if a project ID was passed via startapp parameter (e.g. t.me/bot?startapp=my-project)
    const initData = window.Telegram?.WebApp?.initDataUnsafe;
    if (initData?.start_param) {
      setProjectId(initData.start_param);
      setSearchInput(initData.start_param);
    }
  }, []);

  // Fetch project status
  const fetchStatus = async (pid: string) => {
    setStatus('loading');
    try {
      const res = await fetch(`${API_URL}/api/projects/${pid}/status`);
      const data = await res.json();
      if (data.status === 'live' && data.url) {
        setPreviewUrl(data.url);
        setStatus('live');
      } else {
        setPreviewUrl(null);
        setStatus('offline');
      }
    } catch (err) {
      console.error(err);
      setStatus('offline');
      setPreviewUrl(null);
    }
  };

  useEffect(() => {
    if (!projectId) return;
    
    fetchStatus(projectId);
    
    // Setup Server-Sent Events (SSE) for automatic hot reloading
    const eventSource = new EventSource(`${API_URL}/api/projects/${projectId}/events`);
    
    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.action === 'reload') {
          // Force iframe refresh
          setReloadKey(prev => prev + 1);
          // Optional: haptic feedback via Telegram SDK
          WebApp.HapticFeedback.impactOccurred('light');
        }
      } catch (err) {
        console.error("Failed to parse SSE event:", err);
      }
    };
    
    eventSource.onerror = () => {
      console.log("SSE connection error, retrying...");
    };

    return () => {
      eventSource.close();
    };
  }, [projectId]);

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    if (searchInput.trim()) {
      setProjectId(searchInput.trim());
    }
  };

  const forceReload = () => {
    setReloadKey(prev => prev + 1);
    WebApp.HapticFeedback.impactOccurred('medium');
  };

  return (
    <div className="flex flex-col h-screen bg-tg-secondaryBg text-tg-text font-sans overflow-hidden">
      {/* Header Bar */}
      <header className="flex-none bg-tg-bg border-b border-tg-hint/20 px-4 py-3 flex items-center justify-between z-10 shadow-sm">
        <div className="flex items-center space-x-2 flex-1">
          <Monitor className="text-tg-primary w-5 h-5" />
          <h1 className="font-bold text-lg hidden sm:block">Workbench</h1>
          
          <form onSubmit={handleSearch} className="flex-1 max-w-sm ml-2 relative">
            <Search className="absolute left-2.5 top-2 w-4 h-4 text-tg-hint" />
            <input 
              type="text"
              placeholder="Project Name..."
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              className="w-full bg-tg-secondaryBg text-sm rounded-full pl-9 pr-4 py-1.5 focus:outline-none focus:ring-1 focus:ring-tg-primary border border-transparent transition-all placeholder:text-tg-hint/70 text-tg-text"
            />
          </form>
        </div>

        <div className="flex items-center space-x-3 ml-4">
          <div className="flex items-center text-xs font-medium space-x-1.5 px-2 py-1 rounded bg-tg-secondaryBg">
            <div className={`w-2 h-2 rounded-full ${status === 'live' ? 'bg-green-500 shadow-[0_0_8px_rgba(34,197,94,0.6)] animate-pulse' : status === 'offline' ? 'bg-red-500' : 'bg-yellow-500 animate-bounce'}`} />
            <span className="capitalize hidden xs:inline">{status}</span>
          </div>
          
          <button 
            onClick={forceReload}
            disabled={status !== 'live'}
            className="p-1.5 rounded-full bg-tg-secondaryBg text-tg-primary hover:bg-tg-primary/10 transition-colors disabled:opacity-50"
            title="Manual Reload"
          >
            <RotateCw className="w-4 h-4" />
          </button>
        </div>
      </header>

      {/* Main Content Area */}
      <main className="flex-1 relative flex flex-col bg-slate-900 overflow-hidden">
        {status === 'live' && previewUrl ? (
          <div className="w-full h-full relative" key={`iframe-container-${reloadKey}`}>
            <iframe 
               src={previewUrl}
               className="w-full h-full border-none bg-white"
               title="Project Preview"
               sandbox="allow-scripts allow-same-origin allow-forms allow-modals allow-popups"
            />
          </div>
        ) : (
          <div className="flex-1 flex flex-col items-center justify-center text-tg-hint/50 p-6 text-center">
            {status === 'loading' ? (
               <RotateCw className="w-12 h-12 mb-4 animate-spin opacity-50 text-tg-primary" />
            ) : (
               <LayoutTemplate className="w-16 h-16 mb-4 opacity-40" />
            )}
            <h2 className="text-xl font-semibold text-tg-text/70 mb-2">
              {status === 'offline' ? 'Project Offline' : 'No Project Selected'}
            </h2>
            <p className="text-sm max-w-sm text-tg-hint">
              {status === 'offline' 
                ? "The specified project doesn't have an active Cloudflare Tunnel running. Ask Valentine to preview it first."
                : "Enter a project name above or open this app directly from a Valentine message to see live updates."
              }
            </p>
          </div>
        )}
      </main>

      {/* Footer Attribution */}
      <footer className="flex-none bg-tg-secondaryBg border-t border-tg-hint/20 px-4 py-1.5 flex justify-center items-center z-10 text-[10px] text-tg-hint uppercase tracking-wider font-semibold shadow-[0_-2px_10px_rgba(0,0,0,0.05)]">
        Powered by WDC Solutions
      </footer>
    </div>
  );
}

export default App;
