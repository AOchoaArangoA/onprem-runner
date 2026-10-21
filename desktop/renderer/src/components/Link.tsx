import type { MouseEvent, ReactNode } from 'react';
import { go } from '../lib/guard';
import { href, type Route } from '../lib/router';

export function Link({
  to,
  className,
  children,
  title,
}: {
  to: Route;
  className?: string;
  children: ReactNode;
  title?: string;
}) {
  const onClick = (event: MouseEvent<HTMLAnchorElement>) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault();
    go(to);
  };
  return (
    <a href={href(to)} className={className} onClick={onClick} title={title}>
      {children}
    </a>
  );
}
