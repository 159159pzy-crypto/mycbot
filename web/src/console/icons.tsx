type IconProps = { size?: number };

function frame(size: number) {
  return {
    width: size,
    height: size,
    viewBox: '0 0 18 18',
    fill: 'none',
    'aria-hidden': true as const,
  };
}

export function LockIcon({ size = 16 }: IconProps) {
  return (
    <svg {...frame(size)} className="icon">
      <rect x="3.5" y="8" width="11" height="7" rx="2" />
      <path d="M6 8V6a3 3 0 0 1 6 0v2" />
    </svg>
  );
}

export function StatusIcon({ size = 18 }: IconProps) {
  return (
    <svg {...frame(size)} className="icon">
      <circle cx="9" cy="9" r="6.5" />
      <circle cx="9" cy="9" r="1.9" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function GridIcon({ size = 18 }: IconProps) {
  return (
    <svg {...frame(size)} className="icon">
      <rect x="2.5" y="2.5" width="5.5" height="5.5" rx="1.5" />
      <rect x="10" y="2.5" width="5.5" height="5.5" rx="1.5" />
      <rect x="2.5" y="10" width="5.5" height="5.5" rx="1.5" />
      <rect x="10" y="10" width="5.5" height="5.5" rx="1.5" />
    </svg>
  );
}

export function ChatIcon({ size = 18 }: IconProps) {
  return (
    <svg {...frame(size)} className="icon">
      <rect x="2" y="3" width="14" height="10" rx="3.5" />
      <path d="M6.5 13v2.6L9.8 13" />
    </svg>
  );
}

export function DatabaseIcon({ size = 18 }: IconProps) {
  return (
    <svg {...frame(size)} className="icon">
      <ellipse cx="9" cy="4.6" rx="6" ry="2.1" />
      <path d="M3 4.6v8.8c0 1.16 2.69 2.1 6 2.1s6-0.94 6-2.1V4.6" />
      <path d="M3 9c0 1.16 2.69 2.1 6 2.1S15 10.16 15 9" />
    </svg>
  );
}

export function PlusBoxIcon({ size = 18 }: IconProps) {
  return (
    <svg {...frame(size)} className="icon">
      <rect x="2.5" y="2.5" width="13" height="13" rx="3.5" />
      <path d="M9 6v6M6 9h6" />
    </svg>
  );
}

export function PersonIcon({ size = 18 }: IconProps) {
  return (
    <svg {...frame(size)} className="icon">
      <circle cx="9" cy="6" r="3" />
      <path d="M3.5 15.5c0-3 2.5-4.6 5.5-4.6s5.5 1.6 5.5 4.6" />
    </svg>
  );
}

export function LayersIcon({ size = 20 }: IconProps) {
  return (
    <svg {...frame(size)} className="icon">
      <path d="M2.5 6.2 9 3l6.5 3.2L9 9.4 2.5 6.2Z" />
      <path d="M2.5 9.2 9 12.4l6.5-3.2M2.5 12.2 9 15.4l6.5-3.2" />
    </svg>
  );
}

export function FlaskIcon({ size = 18 }: IconProps) {
  return (
    <svg {...frame(size)} className="icon">
      <path d="M6.5 2.5h5M7.5 2.5v4.2l-4 6.3a1.7 1.7 0 0 0 1.45 2.5h8.1A1.7 1.7 0 0 0 14.5 13l-4-6.3V2.5" />
      <path d="M5.6 11h6.8" />
    </svg>
  );
}

export function ModelIcon({ size = 18 }: IconProps) {
  return (
    <svg {...frame(size)} className="icon">
      <rect x="3" y="3" width="12" height="12" rx="3" />
      <path d="M6 1.5v3M12 1.5v3M6 13.5v3M12 13.5v3M1.5 6h3M13.5 6h3M1.5 12h3M13.5 12h3" />
      <circle cx="9" cy="9" r="2.2" />
    </svg>
  );
}
