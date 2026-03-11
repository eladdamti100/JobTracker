"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { LayoutDashboard, Briefcase, Settings } from "lucide-react";

const NAV = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/applications", label: "Applications", icon: Briefcase },
];

export default function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="w-60 min-h-screen bg-sidebar flex flex-col flex-shrink-0">
      {/* Logo */}
      <div className="px-6 py-5 border-b border-white/10">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 bg-indigo-400 rounded-md flex items-center justify-center">
            <Briefcase className="w-4 h-4 text-white" />
          </div>
          <span className="text-white font-semibold text-sm">JobTracker</span>
        </div>
      </div>

      {/* Nav */}
      <nav className="flex-1 px-3 py-4 space-y-1">
        {NAV.map(({ href, label, icon: Icon }) => {
          const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
          return (
            <Link
              key={href}
              href={href}
              className={`flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors ${
                active
                  ? "bg-sidebar-active text-white"
                  : "text-white/60 hover:text-white hover:bg-sidebar-hover"
              }`}
            >
              <Icon className="w-4 h-4 flex-shrink-0" />
              {label}
            </Link>
          );
        })}
      </nav>

      {/* Footer */}
      <div className="px-3 pb-4">
        <div className="flex items-center gap-2 px-3 py-2 rounded-lg text-white/40 text-xs">
          <Settings className="w-3.5 h-3.5" />
          Phase 2 coming soon
        </div>
      </div>
    </aside>
  );
}
