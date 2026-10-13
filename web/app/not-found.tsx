import Link from "next/link";

export default function NotFound() {
  return (
    <>
      <h1>Not found</h1>
      <p>
        There is nothing at this address. <Link href="/">Back to runs</Link>.
      </p>
    </>
  );
}
