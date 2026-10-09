// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;
import { ERC1967Proxy } from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
// The proxy the BRLC token sits behind on the private demo network (as on the real networks).
contract DevnetProxy is ERC1967Proxy {
    constructor(address implementation, bytes memory data) ERC1967Proxy(implementation, data) {}
}
