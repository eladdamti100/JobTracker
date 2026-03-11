interface Props {
  label: string;
  value: number | string;
  sub?: string;
  accent?: string; // tailwind bg class
  icon?: React.ReactNode;
}

export default function StatsCard({ label, value, sub, accent = "bg-indigo-50", icon }: Props) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5 flex items-start gap-4 shadow-sm">
      {icon && (
        <div className={`w-10 h-10 rounded-lg ${accent} flex items-center justify-center flex-shrink-0`}>
          {icon}
        </div>
      )}
      <div>
        <p className="text-sm text-gray-500 font-medium">{label}</p>
        <p className="text-2xl font-bold text-gray-900 mt-0.5">{value}</p>
        {sub && <p className="text-xs text-gray-400 mt-0.5">{sub}</p>}
      </div>
    </div>
  );
}
