import { useEffect } from "react";

import Scene from "./scene/Scene";
import Overlay from "./ui/Overlay";
import { connect } from "./ws";

export default function App() {
  useEffect(() => connect(), []);

  return (
    <>
      <div className="scene-layer">
        <Scene />
      </div>
      <Overlay />
    </>
  );
}
