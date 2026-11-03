import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = { title: "Sayl" };

// Runs before the first paint so the cached language and theme never flash.
const boot = `try{var p=JSON.parse(localStorage.getItem("sayl.prefs")||"{}");var l=p.language||(navigator.language.toLowerCase().indexOf("ar")===0?"ar":"en");var d=document.documentElement;d.lang=l;d.dir=l==="ar"?"rtl":"ltr";var t=p.theme||"system";if(t==="dark"||(t==="system"&&matchMedia("(prefers-color-scheme: dark)").matches))d.classList.add("dark")}catch(e){}`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" dir="ltr" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: boot }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
