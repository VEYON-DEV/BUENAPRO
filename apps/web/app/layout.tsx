import "./globals.css";

export const metadata = {
  title: "BuenaPro",
  description: "Inteligencia para contrataciones publicas",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="es">
      <body>{children}</body>
    </html>
  );
}
